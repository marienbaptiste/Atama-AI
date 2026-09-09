"""Voice activity detection (spec §9, ADR-006/018).

Silero VAD via onnxruntime on the CPU — deliberately not torch: the GPU belongs to Whisper, and
the whole model is ~2 MB (spec §10b budgets < 0.1 GB for this).

A pure state machine over 32 ms frames, so end-of-turn behaviour and the barge-in guard are
testable without a microphone. The orchestrator owns turn-taking policy (ADR-006), which is why
the same detector serves both "is the student talking to me" and "is the student interrupting".

Model signature verified 2026-09-09 (silero_vad.onnx, v5):
    inputs   input (batch, samples) float32 · state (2, batch, 128) float32 · sr () int64
    outputs  output (batch, 1) speech probability · stateN  (feed back as `state`)
    frames   512 samples at 16 kHz = 32 ms. Silence scored 0.0006; light noise 0.0016.
"""
from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np

FRAME_SAMPLES = 512
SAMPLE_RATE = 16000
FRAME_MS = FRAME_SAMPLES * 1000 // SAMPLE_RATE      # 32
SPEECH_THRESHOLD = 0.5
#: Keep this much audio from before speech was detected, so the first phoneme is not clipped.
PREROLL_MS = 300
MODEL_URL = "https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx"


class Mode(str, Enum):
    """What the tutor is doing, which changes how suspicious we are of incoming audio."""

    LISTENING = "listening"   # waiting for the student
    SPEAKING = "speaking"     # the avatar is talking; only a real interruption counts (ADR-018)


class EventKind(str, Enum):
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"
    PAUSE = "pause"           # a gap mid-utterance — the avatar nods on these (spec §8)


@dataclass
class VadEvent:
    kind: EventKind
    at_ms: float
    audio: np.ndarray | None = None      # the utterance, on SPEECH_END


def ensure_model(path: Path, url: str = MODEL_URL) -> Path:
    """Download the VAD model on first use. ~2 MB; not committed (spec §11: no model weights)."""
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlopen(url, timeout=30)  # noqa: S310 - pinned https URL, constant
    with urllib.request.urlopen(url, timeout=30) as response, open(path, "wb") as f:  # noqa: S310
        f.write(response.read())
    return path


@dataclass
class VoiceActivityDetector:
    """Feed 32 ms frames, read events. One instance per session."""

    model_path: Path
    silence_ms: int = 600
    min_speech_ms: int = 300
    threshold: float = SPEECH_THRESHOLD
    #: While the avatar speaks, the bar for "the student is interrupting" is raised (ADR-018).
    bargein_threshold_factor: float = 2.0
    bargein_min_speech_ms: int = 250
    playback_onset_ignore_ms: int = 150
    #: How long a dip may last before a forming utterance is abandoned. Speech is not continuous.
    onset_tolerance_ms: int = 200
    mode: Mode = Mode.LISTENING

    _session: object | None = field(default=None, init=False, repr=False)
    _state: np.ndarray = field(default=None, init=False, repr=False)  # type: ignore[assignment]
    _clock_ms: float = field(default=0.0, init=False)
    _speech_ms: float = field(default=0.0, init=False)
    _silence_ms_run: float = field(default=0.0, init=False)
    _in_speech: bool = field(default=False, init=False)
    _mode_started_ms: float = field(default=0.0, init=False)
    _utterance: list[np.ndarray] = field(default_factory=list, init=False)
    _preroll: list[np.ndarray] = field(default_factory=list, init=False)
    last_probability: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        import onnxruntime as ort

        ensure_model(self.model_path)
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1          # a VAD must never fight Whisper for cores
        self._session = ort.InferenceSession(str(self.model_path), options,
                                             providers=["CPUExecutionProvider"])
        self.reset()

    @classmethod
    def from_config(cls, cfg, model_path: Path | None = None) -> "VoiceActivityDetector":
        return cls(
            model_path=model_path or (cfg.path("CACHE_DIR") / "models" / "silero_vad.onnx"),
            silence_ms=int(cfg.VAD_SILENCE_MS),
            min_speech_ms=int(cfg.VAD_MIN_SPEECH_MS),
            bargein_threshold_factor=float(cfg.BARGEIN_THRESHOLD_FACTOR),
            bargein_min_speech_ms=int(cfg.BARGEIN_MIN_SPEECH_MS),
            playback_onset_ignore_ms=int(cfg.PLAYBACK_ONSET_IGNORE_MS),
        )

    # ------------------------------------------------------------------ public
    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._speech_ms = 0.0
        self._silence_ms_run = 0.0
        self._in_speech = False
        self._utterance.clear()
        self._preroll.clear()

    def enter(self, mode: Mode) -> None:
        """Tell the detector what the tutor is doing. Resets any part-heard utterance."""
        if mode != self.mode:
            self.mode = mode
            self._mode_started_ms = self._clock_ms
            self._speech_ms = 0.0
            self._silence_ms_run = 0.0
            self._in_speech = False
            self._utterance.clear()

    @property
    def active_threshold(self) -> float:
        """How sure we must be that this is speech, given what the tutor is doing.

        Silero returns a PROBABILITY, capped at 1.0, so the spec's "2x the listening threshold"
        cannot be applied as a multiplication: 0.5 x 2 = 1.0 is unreachable and would silently
        disable barge-in altogether (caught by test, 2026-09-09). The factor instead divides the
        remaining headroom to certainty, which is the same intent — be N times more demanding —
        and always lands below 1: 0.5 -> 0.75 at factor 2, 0.833 at factor 3.
        """
        if self.mode is not Mode.SPEAKING:
            return self.threshold
        factor = max(1.0, self.bargein_threshold_factor)
        return 1.0 - (1.0 - self.threshold) / factor

    @property
    def required_speech_ms(self) -> int:
        return self.bargein_min_speech_ms if self.mode is Mode.SPEAKING else self.min_speech_ms

    def probability(self, frame: np.ndarray) -> float:
        """Speech probability for one 32 ms frame, advancing the model's internal state."""
        chunk = np.asarray(frame, dtype=np.float32).reshape(1, -1)
        out, self._state = self._session.run(  # type: ignore[union-attr]
            None, {"input": chunk, "state": self._state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)}
        )
        self.last_probability = float(out[0][0])
        return self.last_probability

    def push(self, frame: np.ndarray) -> list[VadEvent]:
        """Feed one frame; return whatever that frame decided."""
        frame = np.asarray(frame, dtype=np.float32)
        self._clock_ms += FRAME_MS
        prob = self.probability(frame)
        events: list[VadEvent] = []

        # The loudspeaker's own onset is not the student interrupting (ADR-018).
        if self.mode is Mode.SPEAKING and (self._clock_ms - self._mode_started_ms) < self.playback_onset_ignore_ms:
            return events

        self._remember_preroll(frame)
        if self._in_speech:
            self._utterance.append(frame)

        if prob >= self.active_threshold:
            self._speech_ms += FRAME_MS
            if self._silence_ms_run >= FRAME_MS * 3 and self._in_speech:
                events.append(VadEvent(EventKind.PAUSE, self._clock_ms))
            self._silence_ms_run = 0.0
            if not self._in_speech and self._speech_ms >= self.required_speech_ms:
                self._in_speech = True
                self._utterance = [*self._preroll]
                events.append(VadEvent(EventKind.SPEECH_START, self._clock_ms))
        else:
            self._silence_ms_run += FRAME_MS
            if self._in_speech and self._silence_ms_run >= self.silence_ms:
                audio = np.concatenate(self._utterance) if self._utterance else np.zeros(0, np.float32)
                events.append(VadEvent(EventKind.SPEECH_END, self._clock_ms, audio))
                self._in_speech = False
                self._speech_ms = 0.0
                self._utterance.clear()
            elif not self._in_speech and self._silence_ms_run >= self.onset_tolerance_ms:
                # Only give up on a forming utterance after a SUSTAINED gap. Resetting on the
                # first sub-threshold frame made the 300 ms requirement mean "300 ms with no dip
                # at all", which real speech never satisfies: plosives and gaps between syllables
                # dip below the threshold constantly, so the counter never reached the bar and
                # speech was simply never detected (found in testing, 2026-09-09).
                self._speech_ms = 0.0
        return events

    # ----------------------------------------------------------------- private
    def _remember_preroll(self, frame: np.ndarray) -> None:
        self._preroll.append(frame)
        keep = max(1, PREROLL_MS // FRAME_MS)
        if len(self._preroll) > keep:
            del self._preroll[:-keep]
