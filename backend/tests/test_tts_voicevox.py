"""Emotion -> voice table and the VOICEVOX client (spec §7, ADR-005/020)."""
from __future__ import annotations

import asyncio
import io
import json
import wave
from pathlib import Path

import httpx
import pytest

from backend import config, emotions, tts_voicevox
from backend.chunker import EMOTIONS, NEUTRAL

FX = Path(__file__).parent / "fixtures" / "voicevox"
SPEAKERS = json.loads((FX / "speakers.json").read_text(encoding="utf-8"))
GREETING = json.loads((FX / "greeting.json").read_text(encoding="utf-8"))


def cfg(tmp_path, **env):
    # VOICEVOX_SPEAKER defaults to -1 ("use the persona's voice"); tests that are about a
    # particular speaker say so, rather than inheriting whichever persona is configured.
    return config.load(tmp_path / "settings.json",
                       env={"CACHE_DIR": str(tmp_path), "VOICEVOX_SPEAKER": "29", **env})


# ------------------------------------------------------------------ emotions
def test_styles_are_resolved_by_name_from_the_live_catalogue(tmp_path):
    """No.7 (29) also owns アナウンス=30 and 読み聞かせ=31 — verified against VOICEVOX 0.25.2.

    アナウンス for `serious` is the case a style switch is FOR: same person, different register.
    `happy` deliberately does not take 読み聞かせ even though it is available — see DEFAULT_TABLE.
    """
    table, warnings = emotions.resolve(cfg(tmp_path), SPEAKERS)
    assert warnings == []
    assert table["serious"].style_id == 30 and table["serious"].style_name == "アナウンス"
    assert table[NEUTRAL].style_id == 29
    assert table["happy"].style_id == 29, "happy must not borrow the read-aloud narration voice"


def test_every_emotion_is_present_and_distinct_in_voice(tmp_path):
    """Derived from chunker.EMOTIONS, not a second hand-kept list.

    Adding a tag the tutor can emit without giving it a voice is a silent failure: it would fall
    back to neutral params and sound identical, so the tag would look wired and do nothing.
    """
    table, _ = emotions.resolve(cfg(tmp_path), SPEAKERS)
    assert set(table) == {NEUTRAL, *EMOTIONS}
    signatures = {e: (p.style_id, p.speed, p.pitch, p.intonation) for e, p in table.items()}
    assert len(set(signatures.values())) == len(signatures), signatures


def test_unknown_style_falls_back_to_the_base_and_warns(tmp_path):
    table, warnings = emotions.resolve(cfg(tmp_path, EMOTION_HAPPY="style=ぜんぜんない,speed=1.2"), SPEAKERS)
    assert table["happy"].style_id == 29           # base style, not a crash
    assert table["happy"].speed == 1.2             # scalars still applied (spec §7)
    assert any("ぜんぜんない" in w for w in warnings)


def test_speaker_without_extra_styles_still_works(tmp_path):
    """春日部つむぎ has only ノーマル=8: every emotion collapses to it, scalars intact."""
    table, warnings = emotions.resolve(cfg(tmp_path, VOICEVOX_SPEAKER="8"), SPEAKERS)
    assert {p.style_id for p in table.values()} == {8}
    assert table["surprised"].speed > table["thinking"].speed
    assert warnings == []


def test_config_override_wins_over_the_built_in_table(tmp_path):
    table, _ = emotions.resolve(cfg(tmp_path, EMOTION_SERIOUS="style=30,speed=0.5,pitch=-0.1,intonation=0.7"), SPEAKERS)
    p = table["serious"]
    assert (p.style_id, p.speed, p.pitch, p.intonation) == (30, 0.5, -0.1, 0.7)


def test_apply_multiplies_the_students_baseline_speed():
    params = emotions.VoiceParams(style_id=31, speed=1.05, pitch=0.02, intonation=1.15)
    out = params.apply({"speedScale": 1.0}, base_speed=0.9, base_intonation=1.0)
    assert out["speedScale"] == pytest.approx(0.945)     # a slower learner setting slows every emotion
    assert out["pitchScale"] == pytest.approx(0.02)
    assert out["intonationScale"] == pytest.approx(1.15)


# -------------------------------------------------------------------- client
def fake_wav(ms: float, rate: int = 24000) -> bytes:
    """A real, minimal WAV (16-bit mono silence) of the given length — what VOICEVOX returns,
    shape-wise. `b"RIFFfake"` used to stand in, which made `_wav_duration_ms` 0 and `fitted_to`
    a no-op in every say() test (2026-09-12)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * ms / 1000.0))
    return buf.getvalue()


def transport(handler):
    calls: list[httpx.Request] = []

    def wrapped(req):
        calls.append(req)
        return handler(req)

    return httpx.MockTransport(wrapped), calls


def engine(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path == "/speakers":
        return httpx.Response(200, json=SPEAKERS)
    if path == "/version":
        return httpx.Response(200, json="0.25.2")
    if path == "/audio_query":
        return httpx.Response(200, json=GREETING)
    if path == "/synthesis":
        return httpx.Response(200, content=fake_wav(2300.0), headers={"content-type": "audio/wav"})
    if path == "/initialize_speaker":
        return httpx.Response(204)
    return httpx.Response(404)


def client(tmp_path, handler=engine, **env):
    tr, calls = transport(handler)
    return tts_voicevox.VoicevoxClient.from_config(cfg(tmp_path, **env), transport=tr), calls


def test_say_uses_the_emotions_style_and_returns_a_timeline(tmp_path):
    c, calls = client(tmp_path)
    speech = c.say("こんにちは。", "happy")
    assert speech.style_id == 29 and speech.emotion == "happy"   # base style, brightened by scalars
    assert speech.wav.startswith(b"RIFF") and len(speech.timeline) > 0
    query = [r for r in calls if r.url.path == "/audio_query"][-1]
    assert query.url.params["speaker"] == "29"
    body = json.loads([r for r in calls if r.url.path == "/synthesis"][-1].content)
    assert body["speedScale"] == pytest.approx(0.9 * 1.10)   # learner baseline x emotion
    assert body["intonationScale"] == pytest.approx(1.28)


def test_neutral_uses_the_base_style(tmp_path):
    c, calls = client(tmp_path)
    assert c.say("はい。").style_id == 29


def test_engine_failure_raises_a_clear_error_not_a_stack_trace(tmp_path):
    c, _ = client(tmp_path, lambda r: httpx.Response(200, json=SPEAKERS) if r.url.path in ("/speakers", "/version")
                  else httpx.Response(500, text="boom"))
    with pytest.raises(tts_voicevox.VoicevoxError, match="unreachable or refused"):
        c.say("こんにちは。")


def test_unreachable_engine_leaves_the_client_constructible(tmp_path):
    """Startup must not explode when VOICEVOX is down; the chip reports it instead (§5b)."""
    def dead(req):
        raise httpx.ConnectError("no engine")

    c, _ = client(tmp_path, dead)
    assert c.is_up() is False and c.table          # table still built, from defaults
    assert c.params_for("happy").style_id == 29    # no catalogue -> base speaker
    assert c.table_resolved is False
    assert any("unavailable" in w for w in c.warnings)


def test_an_unknown_catalogue_is_not_a_single_style_speaker(tmp_path):
    """Regression, 2026-09-12: with the engine down at startup `speakers()` was [], every
    emotion fell to the base id, and the table looked exactly like a one-style voice — so the
    1.8x pitch spread was applied to No.7, a multi-style voice, for the whole session."""
    table, warnings = emotions.resolve(cfg(tmp_path, VOICEVOX_SPEAKER="29"), [])
    assert table["serious"].pitch == pytest.approx(-0.03)          # tuned value, not widened
    assert table["surprised"].pitch == pytest.approx(0.09)
    assert {p.style_id for p in table.values()} == {29}
    assert any("unavailable" in w for w in warnings)
    same, _ = emotions.resolve(cfg(tmp_path, VOICEVOX_SPEAKER="29"), None)
    assert same == table


def test_a_late_booting_engine_is_picked_up_at_the_first_synthesis(tmp_path):
    """No restart needed: the table is re-resolved the first time the engine answers."""
    up = {"now": False}

    def flaky(req):
        if not up["now"]:
            raise httpx.ConnectError("still booting")
        return engine(req)

    c, calls = client(tmp_path, flaky)
    assert c.table_resolved is False and c.params_for("serious").style_id == 29
    with pytest.raises(tts_voicevox.VoicevoxError):
        c.say("まだ。")                                # still down: fails, stays unresolved
    assert c.table_resolved is False

    up["now"] = True
    speech = c.say("こんにちは。", "serious")
    assert c.table_resolved is True
    assert speech.style_id == 30                      # アナウンス, from the live catalogue
    assert c.params_for("serious").style_name == "アナウンス"
    before = len(calls)
    c.say("もう一度。", "serious")
    assert [r.url.path for r in calls[before:]] == ["/audio_query", "/synthesis"]   # resolved once


def test_warm_up_re_resolves_a_table_built_blind(tmp_path):
    up = {"now": False}

    def flaky(req):
        if not up["now"]:
            raise httpx.ConnectError("still booting")
        return engine(req)

    c, calls = client(tmp_path, flaky)
    up["now"] = True
    loaded, _ = c.warm_up()
    assert c.table_resolved and loaded > 1            # every style of the resolved table


def test_from_config_async_is_the_same_client_built_off_the_loop(tmp_path):
    tr, calls = transport(engine)
    c = asyncio.run(tts_voicevox.VoicevoxClient.from_config_async(cfg(tmp_path), transport=tr))
    assert c.table_resolved and c.version == "0.25.2"
    assert c.say("はい。").style_id == 29


def test_the_request_timeout_comes_from_config(tmp_path):
    c, _ = client(tmp_path, VOICEVOX_TIMEOUT_S="7.5")
    assert c.timeout_s == 7.5
    assert c.http.timeout.read == 7.5


def test_resolve_is_pure_and_refuses_to_read_the_persona_itself(tmp_path):
    """-1 means "ask the persona"; the file read belongs to the caller, never in here."""
    with pytest.raises(ValueError, match="base_id"):
        emotions.resolve(cfg(tmp_path, VOICEVOX_SPEAKER="-1"), SPEAKERS)
    table, _ = emotions.resolve(cfg(tmp_path, VOICEVOX_SPEAKER="-1"), SPEAKERS, base_id=53)
    assert {p.style_id for p in table.values()} == {53}


def test_wav_duration_is_read_from_the_header():
    assert tts_voicevox._wav_duration_ms(fake_wav(1234.0)) == pytest.approx(1234.0, abs=0.05)
    assert tts_voicevox._wav_duration_ms(fake_wav(0.0)) == 0.0
    assert tts_voicevox._wav_duration_ms(b"RIFFfake") == 0.0
    assert tts_voicevox._wav_duration_ms(b"") == 0.0


def test_say_fits_the_timeline_to_the_real_wav(tmp_path):
    """Gate M2d's mechanism, end to end: the timeline ends exactly with the audio."""
    c, _ = client(tmp_path)
    speech = c.say("こんにちは。")
    assert speech.duration_ms == pytest.approx(2300.0, abs=0.05)
    assert speech.timeline.vtimes[-1] + speech.timeline.vdurations[-1] <= 2300.0 + 1e-6


def test_the_emotions_demo_has_a_line_for_every_emotion(tmp_path):
    """encouraging/proud/confused were added to EMOTIONS and the demo raised KeyError."""
    from backend.tools import voices
    c, _ = client(tmp_path)
    out = voices.emotions_demo(c, tmp_path / "demo.wav")
    assert out.exists() and out.stat().st_size > 44


def test_empty_text_is_refused(tmp_path):
    c, _ = client(tmp_path)
    with pytest.raises(tts_voicevox.VoicevoxError, match="empty"):
        c.say("   ")


def test_a_single_style_speaker_widens_pitch_but_never_intonation(tmp_path):
    """麒ヶ島宗麟 (53) has only ノーマル. With no style to switch to, the emotions ride entirely on
    the scalars — but only pitch may be widened. Widening intonationScale stretches the F0 contour
    and stretches the model's own F0 wobble with it: measured 2026-09-09, intonation 1.0 -> 1.54
    took jitter 2.15% -> 2.41%, which is audible as an unsteady voice between phonemes."""
    table, _ = emotions.resolve(cfg(tmp_path, VOICEVOX_SPEAKER="53"), SPEAKERS)
    assert {p.style_id for p in table.values()} == {53}
    assert abs(table["serious"].pitch) > 0.05                       # widened from -0.03
    assert table["surprised"].intonation == pytest.approx(1.45)     # NOT widened
    assert table["serious"].intonation == pytest.approx(0.85)       # NOT widened
    assert table[NEUTRAL].pitch == 0.0 and table[NEUTRAL].intonation == 1.0   # neutral is the anchor


def test_a_multi_style_speaker_is_left_alone(tmp_path):
    """No.7 expresses emotion by switching style, so the scalars stay as tuned."""
    table, _ = emotions.resolve(cfg(tmp_path, VOICEVOX_SPEAKER="29"), SPEAKERS)
    assert len({p.style_id for p in table.values()}) > 1
    assert table["surprised"].intonation == pytest.approx(1.45)


def test_an_explicit_override_is_never_widened(tmp_path):
    table, _ = emotions.resolve(
        cfg(tmp_path, VOICEVOX_SPEAKER="53", EMOTION_SURPRISED="speed=1.2,pitch=0.01,intonation=1.05"), SPEAKERS)
    p = table["surprised"]
    assert (p.speed, p.pitch, p.intonation) == (1.2, 0.01, 1.05)   # exactly what the user asked for


def test_baseline_pitch_shifts_every_emotion_together():
    """Lowering the register must move the whole voice, not just neutral, or he stops being
    one person."""
    params = emotions.VoiceParams(style_id=53, pitch=0.04)
    out = params.apply({}, 1.0, 1.0, base_pitch=-0.10)
    assert out["pitchScale"] == pytest.approx(-0.06)


def test_sentence_padding_is_applied(tmp_path):
    """VOICEVOX pads 0.1 s at each end; synthesising sentence by sentence turns that into 0.2 s
    of dead air between every sentence (measured)."""
    c, calls = client(tmp_path)
    c.say("こんにちは。")
    body = json.loads([r for r in calls if r.url.path == "/synthesis"][-1].content)
    assert body["prePhonemeLength"] == 0.0 and body["postPhonemeLength"] == pytest.approx(0.08)

# ------------------------------------------------------------------- warm-up
def test_warm_up_preloads_every_distinct_style_in_the_emotion_table(tmp_path):
    """`is_up()` only proves the engine answers; a style's model loads on first use, so the
    opening line would otherwise pay it as silence (spec §5b, VOICEVOX 0.25.2 2026-09-09)."""
    tts, calls = client(tmp_path)
    loaded, elapsed_ms = tts.warm_up()

    init = [c for c in calls if c.url.path == "/initialize_speaker"]
    styles = sorted(int(c.url.params["speaker"]) for c in init)
    assert styles == sorted({p.style_id for p in tts.table.values()})
    assert loaded == len(styles) and len(styles) > 1, "the emotion table spans several styles"
    assert all(c.method == "POST" for c in init)
    # idempotent: never force a reload of a style the engine already holds
    assert all(c.url.params["skip_reinit"] == "true" for c in init)
    assert elapsed_ms >= 0.0


def test_warm_up_is_the_step_that_happens_before_any_synthesis(tmp_path):
    """Ordering is the whole point: nothing is synthesised until every style is loaded."""
    tts, calls = client(tmp_path)
    tts.warm_up()
    tts.say("こんにちは")
    paths = [c.url.path for c in calls]
    assert paths.index("/synthesis") > max(
        i for i, p in enumerate(paths) if p == "/initialize_speaker")


def test_warm_up_raises_when_the_engine_refuses_to_load_a_style(tmp_path):
    def refuses(req):
        if req.url.path == "/initialize_speaker":
            return httpx.Response(500, text="core error")
        return engine(req)

    tts, _ = client(tmp_path, handler=refuses)
    with pytest.raises(tts_voicevox.VoicevoxError) as exc:
        tts.warm_up()
    assert "could not load style" in str(exc.value)
