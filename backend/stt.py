"""Speech to text: faster-whisper, Japanese, local (spec §9, ADR-004).

Local on purpose: a cloud round-trip does not fit the voice->voice budget, and the student's voice never
leaves the machine.

Verified 2026-09-09 on the target box (16 GB RTX-generation laptop GPU, CTranslate2 4.8.2,
faster-whisper 1.2.1):
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
#: (Defaults here; the live values come from config.py — STT_QUIET_RMS and friends, spec §11.)
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
#: Everything `normalise` drops: whitespace and punctuation, anywhere in the string, so the
#: blocklist compares words only. Whisper decorates its hallucinations freely (「…。」, a stray
#: comma, full-width spaces) and each decoration used to be a new phrase to list.
_PUNCTUATION = re.compile(r"[\s。．.！!？?、,・…‥「」『』（）()\[\]【】〜~\-—–ー・:：;；\"'“”‘’]+")


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
    """Words only: whitespace and punctuation removed, so 「ありがとう ございました。」 matches its
    entry and so does the same phrase with a comma in it."""
    return _PUNCTUATION.sub("", str(text or ""))


def blocklisted_phrases(text: str, blocklist: set[str]) -> int:
    """How many blocklisted phrases, laid end to end, make up ALL of `text`. 0 when they do not.

    Whisper's hallucinations come in repeats — 「ご視聴ありがとうございましたご視聴ありがとうご
    ざいました」 on a long stretch of noise — and in pairs (「ご視聴ありがとうございました。チャン
    ネル登録お願いします」). An exact match caught neither. Greedy, longest phrase first: the
    list is small and nothing in it is a prefix of something a student would then continue.
    """
    rest = normalise(text)
    if not rest or not blocklist:
        return 0
    phrases = sorted((ph for ph in blocklist if ph), key=len, reverse=True)
    count = 0
    while rest:
        for ph in phrases:
            if rest.startswith(ph):
                rest = rest[len(ph):]
                count += 1
                break
        else:
            return 0
    return count


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
    #: The filter's thresholds (spec §9; tunable through config.py, spec §11). The rationale for
    #: each default sits on the module constants above.
    quiet_rms: float = QUIET_RMS
    min_avg_logprob: float = MIN_AVG_LOGPROB
    max_no_speech_prob: float = MAX_NO_SPEECH_PROB
    corroborating_avg_logprob: float = CORROBORATING_AVG_LOGPROB
    _model: object | None = field(default=None, init=False, repr=False)
    load_ms: float = field(default=0.0, init=False)
    warmup_ms: float = field(default=0.0, init=False)

    @classmethod
    def from_config(cls, cfg) -> "SpeechToText":
        return cls(model_name=str(cfg.WHISPER_MODEL), compute_type=str(cfg.WHISPER_COMPUTE_TYPE),
                   quiet_rms=float(cfg.STT_QUIET_RMS),
                   min_avg_logprob=float(cfg.STT_MIN_AVG_LOGPROB),
                   max_no_speech_prob=float(cfg.STT_MAX_NO_SPEECH_PROB),
                   corroborating_avg_logprob=float(cfg.STT_CORROBORATING_AVG_LOGPROB))

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

    def listen(self, audio: np.ndarray, quiet_rms: float | None = None) -> Transcript:
        """Transcribe one utterance, discarding what the model clearly invented.

        `quiet_rms` is what near-silence means for the microphone in use (default: the configured
        STT_QUIET_RMS). The fixed level assumes a loud microphone: on a headset whose speech
        arrives at rms 0.003-0.004, every sentence counted as "quiet", so a routine
        no_speech_prob of 0.67 threw real answers away (2026-09-10). The voice loop passes a level
        measured from the room itself.
        """
        if not self.ready:
            raise RuntimeError("SpeechToText.load() was never called")
        if quiet_rms is None:
            quiet_rms = self.quiet_rms
        started = time.monotonic()
        level = rms(audio)
        text, avg_logprob, no_speech = self._transcribe(np.asarray(audio, dtype=np.float32))
        elapsed = (time.monotonic() - started) * 1000.0
        result = Transcript(text=text, accepted=True, avg_logprob=avg_logprob,
                            no_speech_prob=no_speech, rms=level, duration_ms=elapsed)

        if not text:
            return self._reject(result, "empty")
        blocked = blocklisted_phrases(text, self.blocklist)
        if blocked >= 2:
            # Two closing captions back to back is nobody's sentence: whatever the level, this is
            # the model looping on noise.
            return self._reject(result, f"{blocked} blocklisted phrases and nothing else")
        if blocked == 1 and (level < quiet_rms or avg_logprob < self.corroborating_avg_logprob):
            # The phrase alone is not enough: a student really can say ありがとうございました.
            why = "near-silent audio" if level < quiet_rms else f"avg_logprob {avg_logprob:.2f}"
            return self._reject(result, f"blocklisted phrase on {why}")
        if no_speech > self.max_no_speech_prob and (level < quiet_rms or avg_logprob < self.corroborating_avg_logprob):
            why = "quiet audio" if level < quiet_rms else f"avg_logprob {avg_logprob:.2f}"
            return self._reject(result, f"no_speech_prob {no_speech:.2f} + {why}")
        if avg_logprob < self.min_avg_logprob:
            return self._reject(result, f"avg_logprob {avg_logprob:.2f}")
        return result

    @staticmethod
    def _reject(result: Transcript, reason: str) -> Transcript:
        return Transcript(text=result.text, accepted=False, reason=reason,
                          avg_logprob=result.avg_logprob, no_speech_prob=result.no_speech_prob,
                          rms=result.rms, duration_ms=result.duration_ms)
