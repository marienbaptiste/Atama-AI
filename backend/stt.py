"""Speech to text: faster-whisper, Japanese, local (spec §9, ADR-004).

Local on purpose: a cloud round-trip does not fit the 3.0 s budget, and the student's voice never
leaves the machine.

Verified 2026-09-09 on the target box (RTX 4090 mobile, CTranslate2 4.8.2, faster-whisper 1.2.1):
  * `large-v3` @ `int8_float16` on CUDA uses **2 169 MiB** — comfortably under ADR-004's 3.5 GB
    estimate and its 4.5 GB fallback threshold, so `medium` is not needed.
  * CTranslate2 does NOT bundle the CUDA runtime. Without cuBLAS on the DLL search path,
    the model *loads* and then inference dies with "Library cublas64_12.dll is not found".
    `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` provide it; on Windows the directories must be
    registered with `os.add_dll_directory` before the first inference (`enable_cuda_libraries`).
  * The warm-up on one second of silence returned 「ご視聴ありがとうございました」 — which is
    exactly why the hallucination filter below is not optional.
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
DATA_DIR = Path(__file__).parent / "data"
BLOCKLIST_FILE = DATA_DIR / "hallucination_blocklist.txt"
#: Below this RMS the audio is effectively silence, so a confident-sounding transcript is a lie.
QUIET_RMS = 0.012
#: faster-whisper's own confidence signals (spec §9).
MIN_AVG_LOGPROB = -1.0
MAX_NO_SPEECH_PROB = 0.6
#: `no_speech_prob` may not veto on its own — it needs a second opinion (2026-09-09).
#:
#: Measured in a live session: 「台風ではありません。今、ヨーロッパに住んでいる。」 — a coherent,
#: on-topic answer, spoken normally — came back at 0.77 and was thrown away. Whisper's
#: no-speech head is unreliable on utterances that start abruptly, which is every utterance here,
#: because Silero has already trimmed the leading silence off before we hand the audio over.
#:
#: We have a better witness than Whisper anyway: Silero decided this span was speech (ADR-006),
#: and it is a purpose-built detector rather than a by-product of a transcription model. So a high
#: `no_speech_prob` only rejects when something corroborates it — the audio really is near-silent,
#: or the transcript is also weakly predicted. The blocklist rule already works this way, for the
#: same reason: the signal alone is not enough.
CORROBORATING_AVG_LOGPROB = -0.7
_TRAILING = "。．.！!？?、,・…　 \t\r\n"


def enable_cuda_libraries() -> list[str]:
    """Put the pip-installed CUDA runtime on the DLL search path (Windows). Returns what it added.

    CTranslate2 links cuBLAS/cuDNN dynamically but ships neither. The `nvidia-*-cu12` wheels drop
    them under `site-packages/nvidia/*/bin`, which Windows will not search unless we say so.
    """
    added: list[str] = []
    if os.name != "nt":
        return added
    root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not root.is_dir():
        return added
    for directory in dict.fromkeys(p.parent for p in root.rglob("*.dll")):
        try:
            os.add_dll_directory(str(directory))
            added.append(str(directory))
        except (OSError, AttributeError):
            pass
    return added


def load_blocklist(path: Path = BLOCKLIST_FILE) -> set[str]:
    """Phrases Whisper invents on silence. A data file: adding one needs no code change."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {normalise(line) for line in lines if line.strip() and not line.lstrip().startswith("#")}


def normalise(text: str) -> str:
    """Strip whitespace and trailing punctuation so 「ありがとうございました。」 matches its entry."""
    return re.sub(r"\s+", "", str(text or "")).strip(_TRAILING)


def rms(audio: np.ndarray) -> float:
    audio = np.asarray(audio, dtype=np.float32)
    return float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0


@dataclass
class Transcript:
    text: str
    accepted: bool
    reason: str = ""
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    rms: float = 0.0
    duration_ms: float = 0.0

    def __bool__(self) -> bool:
        return self.accepted and bool(self.text)


@dataclass
class SpeechToText:
    """One warm model per session."""

    model_name: str = "large-v3"
    compute_type: str = "int8_float16"
    device: str = "cuda"
    language: str = "ja"
    beam_size: int = 5
    blocklist: set[str] = field(default_factory=load_blocklist)
    _model: object | None = field(default=None, init=False, repr=False)
    load_ms: float = field(default=0.0, init=False)
    warmup_ms: float = field(default=0.0, init=False)

    @classmethod
    def from_config(cls, cfg) -> "SpeechToText":
        return cls(model_name=str(cfg.WHISPER_MODEL), compute_type=str(cfg.WHISPER_COMPUTE_TYPE))

    def load(self) -> None:
        """Load the model and warm it, so the first real turn is not slow (spec §9)."""
        from faster_whisper import WhisperModel

        enable_cuda_libraries()
        started = time.monotonic()
        self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        self.load_ms = (time.monotonic() - started) * 1000.0
        started = time.monotonic()
        self._transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))
        self.warmup_ms = (time.monotonic() - started) * 1000.0

    @property
    def ready(self) -> bool:
        return self._model is not None

    # ---------------------------------------------------------------- transcribe
    def _transcribe(self, audio: np.ndarray) -> tuple[str, float, float]:
        segments, _info = self._model.transcribe(  # type: ignore[union-attr]
            audio, language=self.language, beam_size=self.beam_size,
            condition_on_previous_text=False,   # each utterance stands alone (spec §9)
            vad_filter=False,                   # we run Silero ourselves upstream (ADR-006)
        )
        segments = list(segments)
        text = "".join(s.text for s in segments).strip()
        if not segments:
            return text, 0.0, 1.0
        avg_logprob = sum(s.avg_logprob for s in segments) / len(segments)
        no_speech = max(s.no_speech_prob for s in segments)
        return text, avg_logprob, no_speech

    def listen(self, audio: np.ndarray) -> Transcript:
        """Transcribe one utterance, discarding what the model clearly invented."""
        if not self.ready:
            raise RuntimeError("SpeechToText.load() was never called")
        started = time.monotonic()
        level = rms(audio)
        text, avg_logprob, no_speech = self._transcribe(np.asarray(audio, dtype=np.float32))
        elapsed = (time.monotonic() - started) * 1000.0
        result = Transcript(text=text, accepted=True, avg_logprob=avg_logprob,
                            no_speech_prob=no_speech, rms=level, duration_ms=elapsed)

        if not text:
            return self._reject(result, "empty")
        if normalise(text) in self.blocklist and level < QUIET_RMS:
            # The phrase alone is not enough: a student really can say ありがとうございました.
            return self._reject(result, "blocklisted phrase on near-silent audio")
        if no_speech > MAX_NO_SPEECH_PROB and (level < QUIET_RMS or avg_logprob < CORROBORATING_AVG_LOGPROB):
            why = "quiet audio" if level < QUIET_RMS else f"avg_logprob {avg_logprob:.2f}"
            return self._reject(result, f"no_speech_prob {no_speech:.2f} + {why}")
        if avg_logprob < MIN_AVG_LOGPROB:
            return self._reject(result, f"avg_logprob {avg_logprob:.2f}")
        return result

    @staticmethod
    def _reject(result: Transcript, reason: str) -> Transcript:
        return Transcript(text=result.text, accepted=False, reason=reason,
                          avg_logprob=result.avg_logprob, no_speech_prob=result.no_speech_prob,
                          rms=result.rms, duration_ms=result.duration_ms)
