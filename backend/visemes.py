"""VOICEVOX mora timings -> Oculus viseme timeline (spec §7, ADR-007).

One pure function. TalkingHead has no Japanese text lip-sync module, so we never use
`speakText`: we hand it the exact phoneme timing VOICEVOX already computed.

Shapes verified live against VOICEVOX 0.25.2 on 2026-09-09 (ROADMAP V0.3):

    audio_query = {accent_phrases: [{moras: [...], pause_mora: {...}|null, accent, is_interrogative}],
                   prePhonemeLength, postPhonemeLength, speedScale, pitchScale, intonationScale,
                   pauseLength, pauseLengthScale, volumeScale, outputSamplingRate, outputStereo, kana}
    mora        = {text, consonant: str|null, consonant_length: float|null, vowel, vowel_length, pitch}

`vowel` is one of a/i/u/e/o (voiced), A/I/U/E/O (devoiced — VOICEVOX marks them uppercase),
`N` for ん, `cl` for っ, `pau` for a pause mora. Lengths are seconds at speedScale 1.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Oculus viseme for each vowel. Devoiced vowels use the same shape (see `weights`).
VOWEL_VISEMES = {"a": "aa", "i": "I", "u": "U", "e": "E", "o": "O"}
#: Consonant -> viseme. Longest match first, so "sh"/"ts"/"ch" beat "s"/"t"/"c".
CONSONANT_VISEMES = {
    "k": "kk", "g": "kk",
    "s": "SS", "z": "SS", "sh": "SS", "j": "SS", "ts": "SS",
    "t": "DD", "d": "DD",
    "ch": "CH",
    "n": "nn", "ny": "nn",
    "m": "PP", "b": "PP", "p": "PP", "my": "PP", "by": "PP", "py": "PP",
    "f": "FF", "h": "FF", "hy": "FF",
    "r": "RR", "ry": "RR",
    # w and y are deliberately absent: the following vowel dominates the mouth shape (spec §7).
}
SILENCE = "sil"
#: A devoiced vowel is shaped but barely voiced; the frontend caps its weight (spec §7).
DEVOICED_WEIGHT = 0.4
FULL_WEIGHT = 1.0


@dataclass(frozen=True)
class VisemeTimeline:
    """What TalkingHead's `speakAudio` needs, alongside the WAV.

    Times and durations are in MILLISECONDS from the start of the audio, at full float
    precision — adjacent visemes must join exactly, so rounding happens only in `as_message`.
    (Spec §7 pins ms; confirm against the TalkingHead README at ROADMAP V0.4 before the avatar
    lands — a wrong unit here is silent drift, not an error.)
    """

    visemes: list[str]
    vtimes: list[float]
    vdurations: list[float]
    weights: list[float]
    duration_ms: float

    def __len__(self) -> int:
        return len(self.visemes)

    def fitted_to(self, wav_ms: float) -> "VisemeTimeline":
        """This timeline stretched so it ends exactly with the audio. Pure; returns a new one.

        `build()` predicts duration from the audio_query, but VOICEVOX's synthesis quantises in a
        text-dependent way: measured 2026-09-10, the prediction runs anywhere from -11 ms to
        +46 ms against the real WAV, and no arithmetic on the query can recover the difference.
        Left alone it is cumulative drift — the mouth is progressively later through a sentence
        and worst at the end, which is where it shows.

        The caller knows the true length the instant it has the WAV, so the honest fix is to fit
        the timeline to it and spread the error proportionally: every viseme lands within a
        fraction of a frame instead of the last one being 46 ms out.
        """
        if wav_ms <= 0 or self.duration_ms <= 0:
            return self
        k = wav_ms / self.duration_ms
        return VisemeTimeline(visemes=list(self.visemes),
                              vtimes=[t * k for t in self.vtimes],
                              vdurations=[d * k for d in self.vdurations],
                              weights=list(self.weights),
                              duration_ms=wav_ms)

    def as_message(self, ndigits: int = 3) -> dict[str, Any]:
        return {"visemes": self.visemes,
                "vtimes": [round(t, ndigits) for t in self.vtimes],
                "vdurations": [round(d, ndigits) for d in self.vdurations],
                "weights": self.weights}


def consonant_viseme(consonant: str | None) -> str | None:
    """Viseme for a consonant, or None when it should be skipped (w, y, unknown)."""
    if not consonant:
        return None
    return CONSONANT_VISEMES.get(consonant.lower())


def vowel_viseme(vowel: str | None) -> tuple[str, float]:
    """(viseme, weight) for a vowel phoneme. Handles ん, っ, pauses and devoicing."""
    if not vowel:
        return SILENCE, FULL_WEIGHT
    if vowel == "N":
        return "nn", FULL_WEIGHT           # ん — a real phoneme, not a devoiced vowel
    if vowel in ("cl", "pau"):
        return SILENCE, FULL_WEIGHT        # っ and pause mora
    lowered = vowel.lower()
    if lowered in VOWEL_VISEMES:
        # Uppercase means devoiced: same shape, less of it.
        weight = DEVOICED_WEIGHT if vowel.isupper() else FULL_WEIGHT
        return VOWEL_VISEMES[lowered], weight
    return SILENCE, FULL_WEIGHT


def build(audio_query: dict[str, Any]) -> VisemeTimeline:
    """Walk the morae, accumulating time, and emit the viseme timeline.

    `speedScale` divides every duration, because VOICEVOX applies it when synthesising — the
    query holds lengths at speed 1. `prePhonemeLength` is the leading silence and is scaled the
    same way (verified against real WAV lengths, ROADMAP V0.3).
    """
    speed = float(audio_query.get("speedScale") or 1.0) or 1.0
    visemes: list[str] = []
    vtimes: list[float] = []
    vdurations: list[float] = []
    weights: list[float] = []

    def emit(viseme: str, start_s: float, length_s: float, weight: float) -> None:
        if length_s <= 0:
            return
        visemes.append(viseme)
        vtimes.append(start_s * 1000.0)
        vdurations.append(length_s * 1000.0)
        weights.append(weight)

    t = float(audio_query.get("prePhonemeLength") or 0.0) / speed

    for phrase in audio_query.get("accent_phrases") or []:
        if not isinstance(phrase, dict):
            continue
        for mora in phrase.get("moras") or []:
            if not isinstance(mora, dict):
                continue
            consonant_len = float(mora.get("consonant_length") or 0.0) / speed
            if consonant_len > 0:
                shape = consonant_viseme(mora.get("consonant"))
                if shape is not None:
                    emit(shape, t, consonant_len, FULL_WEIGHT)
                t += consonant_len          # time passes even when the shape is skipped
            vowel_len = float(mora.get("vowel_length") or 0.0) / speed
            shape, weight = vowel_viseme(mora.get("vowel"))
            emit(shape, t, vowel_len, weight)
            t += vowel_len
        pause = phrase.get("pause_mora")
        if isinstance(pause, dict):
            pause_len = float(pause.get("vowel_length") or 0.0) / speed
            emit(SILENCE, t, pause_len, FULL_WEIGHT)
            t += pause_len

    t += float(audio_query.get("postPhonemeLength") or 0.0) / speed
    return VisemeTimeline(visemes, vtimes, vdurations, weights, t * 1000.0)
