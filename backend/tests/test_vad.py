"""VAD state machine: end-of-turn, pauses, and the self-barge-in guard (spec §9, ADR-018).

Frames are synthetic, so these run without a microphone. The ONNX model is real but is fed
generated audio, and the probability function is stubbed where the test is about the state
machine rather than about Silero's judgement.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend import vad as vad_mod
from backend.vad import EventKind, Mode, VoiceActivityDetector

FRAME = vad_mod.FRAME_SAMPLES
MS = vad_mod.FRAME_MS


class FakeVad(VoiceActivityDetector):
    """Drives the state machine from a scripted probability, not from Silero."""

    def __post_init__(self) -> None:      # skip ONNX entirely
        self.mode = Mode.LISTENING
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self.reset()
        self._mode_started_ms = 0.0
        self.scripted = 0.0

    def probability(self, frame):
        self.last_probability = self.scripted
        return self.scripted


def detector(**kw) -> FakeVad:
    return FakeVad(model_path=None, **kw)  # type: ignore[arg-type]


class BareVad(VoiceActivityDetector):
    """Real `probability()`, no ONNX load — for testing what is fed to the model."""

    def __post_init__(self) -> None:
        self.mode = Mode.LISTENING
        self.reset()
        self._mode_started_ms = 0.0


def bare() -> BareVad:
    return BareVad(model_path=None)  # type: ignore[arg-type]


def feed(v: FakeVad, prob: float, ms: int):
    """Push `ms` worth of frames at a given speech probability; collect the events."""
    events = []
    for _ in range(max(1, ms // MS)):
        v.scripted = prob
        events += v.push(np.full(FRAME, 0.1, dtype=np.float32))
    return events


def kinds(events):
    return [e.kind for e in events]


# ------------------------------------------------------------------ end of turn
def test_speech_then_silence_ends_the_turn_exactly_once():
    v = detector(silence_ms=600, min_speech_ms=300)
    assert EventKind.SPEECH_START in kinds(feed(v, 0.9, 400))
    mid = feed(v, 0.9, 500)
    assert EventKind.SPEECH_END not in kinds(mid)
    ends = feed(v, 0.0, 700)
    assert kinds(ends).count(EventKind.SPEECH_END) == 1


def test_a_short_blip_never_becomes_a_turn():
    v = detector(min_speech_ms=300)
    assert feed(v, 0.9, 200) == []            # 200 ms of speech is not an utterance
    assert kinds(feed(v, 0.0, 800)) == []


def test_a_gap_mid_utterance_does_not_split_the_turn():
    """A 400 ms pause is thinking, not the end of a sentence (spec §9)."""
    v = detector(silence_ms=600, min_speech_ms=300)
    feed(v, 0.9, 400)
    events = feed(v, 0.0, 400) + feed(v, 0.9, 300)
    assert EventKind.SPEECH_END not in kinds(events)
    assert EventKind.PAUSE in kinds(events)   # the avatar nods on these (spec §8)
    assert kinds(feed(v, 0.0, 700)).count(EventKind.SPEECH_END) == 1


def test_utterance_audio_is_returned_with_preroll():
    v = detector(min_speech_ms=300, silence_ms=600)
    feed(v, 0.9, 500)
    end = [e for e in feed(v, 0.0, 700) if e.kind is EventKind.SPEECH_END][0]
    assert end.audio is not None and end.audio.size > 0
    # Pre-roll means the utterance starts BEFORE the frame that crossed the threshold,
    # so the first phoneme is not clipped.
    assert end.audio.size > (500 // MS) * FRAME


# ------------------------------------------------------- self-barge-in (ADR-018)
def test_the_avatars_own_voice_does_not_end_its_turn():
    """Speech at the listening threshold must not trigger while the avatar is speaking."""
    v = detector(bargein_threshold_factor=2.0, bargein_min_speech_ms=250)
    v.enter(Mode.SPEAKING)
    assert feed(v, 0.6, 1000) == []           # above 0.5, below the raised bar (0.75)


def test_a_real_interruption_still_gets_through():
    v = detector(bargein_threshold_factor=2.0, bargein_min_speech_ms=250)
    v.enter(Mode.SPEAKING)
    assert EventKind.SPEECH_START in kinds(feed(v, 0.99, 400))


def test_playback_onset_is_ignored():
    """The loudspeaker's first moments are not the student interrupting."""
    v = detector(playback_onset_ignore_ms=150, bargein_min_speech_ms=100, bargein_threshold_factor=1.0)
    v.enter(Mode.SPEAKING)
    assert feed(v, 0.99, 128) == []           # inside the ignore window
    assert EventKind.SPEECH_START in kinds(feed(v, 0.99, 300))


def test_thresholds_switch_with_mode():
    v = detector(threshold=0.5, bargein_threshold_factor=2.0, min_speech_ms=300, bargein_min_speech_ms=250)
    assert (v.active_threshold, v.required_speech_ms) == (0.5, 300)
    v.enter(Mode.SPEAKING)
    assert (v.active_threshold, v.required_speech_ms) == (0.75, 250)


@pytest.mark.parametrize("factor", [1.0, 1.5, 2.0, 3.0, 10.0])
def test_the_bargein_threshold_stays_reachable(factor):
    """Silero returns a probability <= 1.0. A multiplicative factor would put the bar at or above
    1.0 and disable barge-in silently, so the factor divides the headroom instead."""
    v = detector(threshold=0.5, bargein_threshold_factor=factor)
    v.enter(Mode.SPEAKING)
    assert 0.5 <= v.active_threshold < 1.0
    v2 = detector(threshold=0.5, bargein_threshold_factor=factor, bargein_min_speech_ms=100)
    v2.enter(Mode.SPEAKING)
    assert EventKind.SPEECH_START in kinds(feed(v2, 0.999, 400))   # certainty always gets through


def test_entering_a_mode_drops_a_part_heard_utterance():
    v = detector(min_speech_ms=300)
    feed(v, 0.9, 400)
    v.enter(Mode.SPEAKING)
    assert v._in_speech is False


# ----------------------------------------------------------------- the real model
@pytest.mark.skipif(not (vad_mod.Path(".cache/models/silero_vad.onnx").exists()),
                    reason="VAD model not downloaded")
def test_real_silero_scores_silence_low_and_is_deterministic():
    v = VoiceActivityDetector(model_path=vad_mod.Path(".cache/models/silero_vad.onnx"))
    silence = np.zeros(FRAME, dtype=np.float32)
    assert v.probability(silence) < 0.1
    v.reset()
    first = v.probability(silence)
    v.reset()
    assert v.probability(silence) == pytest.approx(first)   # reset really resets the state


# ------------------------------------------------- onset tolerance (real speech dips)
def test_a_dip_between_syllables_does_not_abandon_a_forming_utterance():
    """Real speech is not 300 ms of uninterrupted probability. Plosives and syllable gaps dip
    below the threshold constantly; resetting on the first one meant speech was NEVER detected
    (found in live testing, 2026-09-09)."""
    v = detector(min_speech_ms=300, onset_tolerance_ms=200)
    events = []
    for _ in range(5):                     # 5 x (64 ms speech + 64 ms dip) = 320 ms of speech
        events += feed(v, 0.9, 64)
        events += feed(v, 0.1, 64)
    assert EventKind.SPEECH_START in kinds(events)


def test_a_sustained_gap_still_abandons_it():
    v = detector(min_speech_ms=300, onset_tolerance_ms=200)
    feed(v, 0.9, 200)                      # not yet enough to start
    feed(v, 0.0, 300)                      # a real gap, past the tolerance
    assert v._speech_ms == 0.0
    assert feed(v, 0.9, 200) == []         # the counter restarted, so 200 ms is not enough


# ------------------------------------------------- Silero's context window (v5)
def test_the_model_is_fed_the_context_window_not_a_bare_frame():
    """v5 prepends 64 samples of the previous frame. The ONNX graph takes a dynamic width, so
    feeding 512 does NOT error — it just returns ~0.002 for everything, including loud speech.
    That looked exactly like a broken microphone (2026-09-09)."""
    seen = {}

    class SpySession:
        def run(self, _outputs, feeds):
            seen["width"] = feeds["input"].shape[-1]
            seen["state"] = feeds["state"].shape
            seen["sr"] = int(feeds["sr"])
            return np.array([[0.9]], dtype=np.float32), np.zeros((2, 1, 128), dtype=np.float32)

    v = bare()
    v._session = SpySession()
    v.probability(np.full(FRAME, 0.2, dtype=np.float32))
    assert seen["width"] == FRAME + vad_mod.CONTEXT_SAMPLES == 576
    assert seen["state"] == (2, 1, 128) and seen["sr"] == 16000


def test_the_context_carries_the_tail_of_the_previous_frame():
    windows = []

    class SpySession:
        def run(self, _outputs, feeds):
            windows.append(feeds["input"][0].copy())
            return np.array([[0.0]], dtype=np.float32), np.zeros((2, 1, 128), dtype=np.float32)

    v = bare()
    v._session = SpySession()
    first = np.linspace(0, 1, FRAME, dtype=np.float32)
    second = np.full(FRAME, -0.5, dtype=np.float32)
    v.probability(first)
    v.probability(second)
    assert np.allclose(windows[0][:vad_mod.CONTEXT_SAMPLES], 0.0)          # first frame: silence
    assert np.allclose(windows[1][:vad_mod.CONTEXT_SAMPLES], first[-vad_mod.CONTEXT_SAMPLES:])
    assert np.allclose(windows[1][vad_mod.CONTEXT_SAMPLES:], second)


REAL_SPEECH = vad_mod.Path(__file__).parent / "fixtures" / "audio" / "japanese_speech_16k.wav"


@pytest.mark.skipif(not (REAL_SPEECH.exists() and vad_mod.Path(".cache/models/silero_vad.onnx").exists()),
                    reason="speech fixture or VAD model unavailable")
@pytest.mark.parametrize("scale,label", [(1.0, "full volume"), (0.05, "a quiet microphone")])
def test_real_japanese_speech_is_detected(scale, label):
    """The end-to-end guard: real speech in, a complete utterance out. Both at full volume and
    at the level a quiet headset actually delivers (peak ~0.02)."""
    from backend import audio as audio_mod

    speech, rate = audio_mod.decode_wav(REAL_SPEECH.read_bytes())
    assert rate == 16000
    padded = np.concatenate([np.zeros(16000, np.float32),
                             np.clip(speech * scale, -1, 1).astype(np.float32),
                             np.zeros(16000, np.float32)])
    v = VoiceActivityDetector(model_path=vad_mod.Path(".cache/models/silero_vad.onnx"))
    events = []
    for i in range(0, len(padded) - FRAME, FRAME):
        events += v.push(padded[i:i + FRAME])
    got = kinds(events)
    assert EventKind.SPEECH_START in got, f"{label}: no speech detected"
    assert EventKind.SPEECH_END in got, f"{label}: utterance never ended"
    end = [e for e in events if e.kind is EventKind.SPEECH_END][0]
    assert end.audio is not None and end.audio.size > 16000     # the utterance came back with it


def test_preroll_reaches_back_across_the_whole_onset_window(tmp_path):
    """Regression, 2026-09-09: every sentence lost its opening.

    SPEECH_START fires once *accumulated* speech passes `min_speech_ms`, but that accumulation
    spans far more wall clock — dips between syllables do not count toward it, and each may run
    `onset_tolerance_ms`. A flat 300 ms ring had already slid past the true start by then.
    """
    v = vad_mod.VoiceActivityDetector(model_path=tmp_path / "m.onnx",
                                      min_speech_ms=300, onset_tolerance_ms=200)
    assert v.preroll_ms() >= v.required_speech_ms + v.onset_tolerance_ms
    assert v.preroll_ms() > vad_mod.PREROLL_MS          # the old flat constant was not enough

    # It tracks the thresholds instead of drifting from them.
    slow = vad_mod.VoiceActivityDetector(model_path=tmp_path / "m.onnx",
                                         min_speech_ms=800, onset_tolerance_ms=500)
    assert slow.preroll_ms() > v.preroll_ms()
