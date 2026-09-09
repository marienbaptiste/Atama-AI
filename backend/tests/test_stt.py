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
def test_high_no_speech_probability_is_rejected():
    result = speaker("なにか", no_speech=0.9).listen(loud())
    assert not result and "no_speech_prob" in result.reason


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
    assert stt_mod.rms(np.array([], dtype=np.float32)) == 0.0


def test_transcript_is_falsy_when_rejected():
    assert not Transcript(text="x", accepted=False)
    assert Transcript(text="x", accepted=True)
