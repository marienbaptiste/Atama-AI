"""STT hallucination filter and thresholds (spec §9, ADR-004).

The model itself is stubbed: these are about the filter, which is the part that decides whether
the tutor answers something the student never said.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend import stt as stt_mod
from backend.stt import SpeechToText, Transcript, normalise


def speaker(text: str, avg_logprob: float = -0.2, no_speech: float = 0.05) -> SpeechToText:
    s = SpeechToText()
    s._model = object()                     # marks it "ready" without loading anything
    s._transcribe = lambda audio: (text, avg_logprob, no_speech)  # type: ignore[assignment]
    return s


def quiet(seconds: float = 1.0) -> np.ndarray:
    return np.zeros(int(stt_mod.SAMPLE_RATE * seconds), dtype=np.float32)


def loud(seconds: float = 1.0) -> np.ndarray:
    rng = np.random.default_rng(0)
    return (rng.standard_normal(int(stt_mod.SAMPLE_RATE * seconds)) * 0.2).astype(np.float32)


# ------------------------------------------------------------------- blocklist
def test_blocklist_is_a_data_file_that_loads():
    blocklist = stt_mod.load_blocklist()
    assert normalise("ご視聴ありがとうございました") in blocklist
    assert len(blocklist) > 5


def test_the_hallucination_whisper_actually_produced_is_rejected():
    """Verified live 2026-09-09: the warm-up on 1 s of zeros returned exactly this."""
    result = speaker("ご視聴ありがとうございました").listen(quiet())
    assert not result and "blocklisted" in result.reason


def test_trailing_punctuation_does_not_defeat_the_blocklist():
    assert not speaker("ご視聴ありがとうございました。").listen(quiet())
    assert not speaker("  おやすみなさい　").listen(quiet())


def test_the_same_phrase_spoken_aloud_is_kept():
    """A student really can say ありがとうございました — the filter needs the audio to be quiet too."""
    result = speaker("ありがとうございました").listen(loud())
    assert result and result.accepted


def test_adding_a_phrase_needs_no_code_change(tmp_path):
    path = tmp_path / "blocklist.txt"
    path.write_text("# comment\n\nよろしくお願いします\n", encoding="utf-8")
    s = speaker("よろしくお願いします。")
    s.blocklist = stt_mod.load_blocklist(path)
    assert not s.listen(quiet())


def test_missing_blocklist_file_degrades_to_empty(tmp_path):
    assert stt_mod.load_blocklist(tmp_path / "absent.txt") == set()


# ------------------------------------------------------------------ thresholds
def test_high_no_speech_probability_alone_does_not_reject_confident_loud_speech():
    """Regression, 2026-09-09: 「台風ではありません。今、ヨーロッパに住んでいる。」 — a coherent,
    on-topic answer spoken normally — scored 0.77 and was thrown away. Whisper's no-speech head is
    unreliable on utterances that start abruptly, which is all of ours: Silero has already trimmed
    the leading silence. Silero is the better witness, so no_speech_prob needs corroboration."""
    result = speaker("なにか", no_speech=0.9, avg_logprob=-0.2).listen(loud())
    assert result, result.reason


def test_high_no_speech_probability_rejects_when_the_audio_is_quiet():
    result = speaker("なにか", no_speech=0.9, avg_logprob=-0.2).listen(quiet())
    assert not result and "no_speech_prob" in result.reason and "quiet" in result.reason


def test_high_no_speech_probability_rejects_when_the_text_is_also_weakly_predicted():
    """Neither signal is damning alone; together they are."""
    result = speaker("なにか", no_speech=0.9, avg_logprob=-0.9).listen(loud())
    assert not result and "no_speech_prob" in result.reason and "avg_logprob" in result.reason


def test_low_confidence_is_rejected():
    result = speaker("なにか", avg_logprob=-2.5).listen(loud())
    assert not result and "avg_logprob" in result.reason


def test_empty_transcript_is_rejected():
    assert not speaker("").listen(loud())


def test_a_good_transcript_survives_every_check():
    result = speaker("こんにちは、元気ですか。").listen(loud())
    assert result and result.text == "こんにちは、元気ですか。" and result.reason == ""


def test_rejections_keep_the_text_for_the_log():
    """A discarded transcript is still worth seeing when tuning the filter."""
    result = speaker("ご視聴ありがとうございました").listen(quiet())
    assert result.text and result.accepted is False


def test_listen_before_load_is_a_clear_error():
    with pytest.raises(RuntimeError, match="load"):
        SpeechToText().listen(quiet())


# --------------------------------------------------------------------- helpers
def test_rms_distinguishes_silence_from_speech():
    assert stt_mod.rms(quiet()) < stt_mod.QUIET_RMS < stt_mod.rms(loud())
    assert SpeechToText().quiet_rms == stt_mod.QUIET_RMS
    assert stt_mod.rms(np.array([], dtype=np.float32)) == 0.0


def test_transcript_is_falsy_when_rejected():
    assert not Transcript(text="x", accepted=False)
    assert Transcript(text="x", accepted=True)


def test_a_quiet_headset_is_judged_against_its_own_room_not_a_fixed_level():
    """2026-09-10: speech at rms ~0.003 counted as "quiet" by the fixed 0.012, so a routine
    no_speech_prob of 0.67 threw a real answer away. Measured against the room, it is speech."""
    import numpy as np
    soft = (np.sin(np.arange(16000) / 7.0) * 0.0045).astype(np.float32)   # rms about 0.003
    s = speaker("元気です、いかがですか", no_speech=0.67, avg_logprob=-0.3)
    assert not s.listen(soft)                            # the fixed rule: rejected
    assert s.listen(soft, quiet_rms=0.0001)              # the room's own level: accepted


# --------------------------------------------------- the blocklist, properly (2026-09-12)
def test_punctuation_anywhere_does_not_defeat_the_blocklist():
    assert not speaker("ご視聴、ありがとう ございました…。").listen(quiet())
    assert normalise("「ご視聴」ありがとうございました！") == "ご視聴ありがとうございました"


def test_the_same_phrase_repeated_is_rejected_whatever_the_level():
    """Whisper loops on noise: the caption comes back two, three, ten times. Nobody says that."""
    twice = "ご視聴ありがとうございました。ご視聴ありがとうございました。"
    result = speaker(twice).listen(loud())
    assert not result and "2 blocklisted phrases" in result.reason
    assert not speaker("バイバイ バイバイ バイバイ").listen(loud())


def test_two_different_captions_back_to_back_are_rejected_too():
    assert not speaker("ご視聴ありがとうございました。チャンネル登録お願いします。").listen(loud())


def test_a_blocklisted_phrase_with_real_words_around_it_is_speech():
    assert speaker("ありがとうございました、先生。").listen(loud())
    assert speaker("ご視聴ありがとうございました、と言いました").listen(quiet())


def test_a_single_caption_on_weakly_predicted_audio_is_rejected_even_when_loud():
    """The data file always said "quiet OR poor confidence"; the code only checked quiet."""
    result = speaker("ご視聴ありがとうございました", avg_logprob=-0.9).listen(loud())
    assert not result and "blocklisted" in result.reason and "avg_logprob" in result.reason
    assert speaker("ご視聴ありがとうございました", avg_logprob=-0.2).listen(loud())


def test_blocklisted_phrases_counts_end_to_end_runs_only():
    bl = {"ありがとうございました", "ご視聴ありがとうございました", "バイバイ"}
    assert stt_mod.blocklisted_phrases("ご視聴ありがとうございましたバイバイ", bl) == 2
    assert stt_mod.blocklisted_phrases("バイバイ！バイバイ！", bl) == 2
    assert stt_mod.blocklisted_phrases("ありがとうございました", bl) == 1
    assert stt_mod.blocklisted_phrases("ありがとうございましたね", bl) == 0
    assert stt_mod.blocklisted_phrases("", bl) == 0 and stt_mod.blocklisted_phrases("x", set()) == 0


def test_the_data_file_has_no_duplicates_and_no_stray_entries():
    raw = [ln.strip() for ln in stt_mod.BLOCKLIST_FILE.read_text(encoding="utf-8").splitlines()
           if ln.strip() and not ln.lstrip().startswith("#")]
    normalised = [normalise(ln) for ln in raw]
    assert len(set(normalised)) == len(normalised), "a phrase is listed twice"
    assert [ln.replace(" ", "") for ln in raw] == normalised, "list the words only: matching ignores punctuation anyway"
    japanese = lambda ln: any("\u3040" <= ch <= "\u9fff" for ch in ln)   # noqa: E731
    assert all(japanese(ln) or ln.isascii() for ln in raw), "a stray non-Japanese, non-English token (эн was one)"


def test_thresholds_resolve_through_config(tmp_path):
    from backend import config
    cfg = config.load(tmp_path / "s.json", env={"STT_QUIET_RMS": "0.02", "STT_MIN_AVG_LOGPROB": "-1.5",
                                                "STT_MAX_NO_SPEECH_PROB": "0.8",
                                                "STT_CORROBORATING_AVG_LOGPROB": "-0.5"})
    s = SpeechToText.from_config(cfg)
    assert (s.quiet_rms, s.min_avg_logprob, s.max_no_speech_prob, s.corroborating_avg_logprob) == (0.02, -1.5, 0.8, -0.5)
    s._model = object()
    s._transcribe = lambda audio: ("なにか", -1.2, 0.1)   # type: ignore[assignment]
    assert s.listen(loud())                                  # -1.2 is above the configured -1.5: kept
    s._transcribe = lambda audio: ("なにか", -1.6, 0.1)   # type: ignore[assignment]
    assert not s.listen(loud())
