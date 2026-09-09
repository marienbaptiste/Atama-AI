"""Emotion -> voice table and the VOICEVOX client (spec §7, ADR-005/020)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend import config, emotions, tts_voicevox
from backend.chunker import NEUTRAL

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
    """No.7 (29) also owns アナウンス=30 and 読み聞かせ=31 — verified against VOICEVOX 0.25.2."""
    table, warnings = emotions.resolve(cfg(tmp_path), SPEAKERS)
    assert warnings == []
    assert table["happy"].style_id == 31 and table["happy"].style_name == "読み聞かせ"
    assert table["serious"].style_id == 30 and table["serious"].style_name == "アナウンス"
    assert table[NEUTRAL].style_id == 29


def test_every_emotion_is_present_and_distinct_in_voice(tmp_path):
    table, _ = emotions.resolve(cfg(tmp_path), SPEAKERS)
    assert set(table) == {NEUTRAL, "happy", "thinking", "surprised", "serious"}
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
        return httpx.Response(200, content=b"RIFFfake", headers={"content-type": "audio/wav"})
    if path == "/initialize_speaker":
        return httpx.Response(204)
    return httpx.Response(404)


def client(tmp_path, handler=engine, **env):
    tr, calls = transport(handler)
    return tts_voicevox.VoicevoxClient.from_config(cfg(tmp_path, **env), transport=tr), calls


def test_say_uses_the_emotions_style_and_returns_a_timeline(tmp_path):
    c, calls = client(tmp_path)
    speech = c.say("こんにちは。", "happy")
    assert speech.style_id == 31 and speech.emotion == "happy"
    assert speech.wav.startswith(b"RIFF") and len(speech.timeline) > 0
    query = [r for r in calls if r.url.path == "/audio_query"][-1]
    assert query.url.params["speaker"] == "31"
    body = json.loads([r for r in calls if r.url.path == "/synthesis"][-1].content)
    assert body["speedScale"] == pytest.approx(0.9 * 1.05)   # learner baseline x emotion
    assert body["intonationScale"] == pytest.approx(1.15)


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
    assert table["surprised"].intonation == pytest.approx(1.30)     # NOT widened
    assert table["serious"].intonation == pytest.approx(0.85)       # NOT widened
    assert table[NEUTRAL].pitch == 0.0 and table[NEUTRAL].intonation == 1.0   # neutral is the anchor


def test_a_multi_style_speaker_is_left_alone(tmp_path):
    """No.7 expresses emotion by switching style, so the scalars stay as tuned."""
    table, _ = emotions.resolve(cfg(tmp_path, VOICEVOX_SPEAKER="29"), SPEAKERS)
    assert len({p.style_id for p in table.values()}) > 1
    assert table["surprised"].intonation == pytest.approx(1.30)


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
