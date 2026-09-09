"""Emotion -> voice parameters (spec §7, ADR-020).

One tag drives both the face and the voice. This module owns the voice half: which VOICEVOX
style to synthesise with, and how to bend speed, pitch and intonation.

Style ids are resolved at startup from the live `GET /speakers`, by NAME, so the table survives
a VOICEVOX upgrade renumbering ids. A style that does not exist for the configured speaker falls
back to that speaker's base style with the scalar overrides still applied — the tutor keeps
speaking, just less expressively (spec §7).

Verified against VOICEVOX 0.25.2, 2026-09-09: speaker "No.7" has ノーマル=29, アナウンス=30,
読み聞かせ=31 — a base, a crisp formal one and a warm read-aloud one, which is a good spread for
a teacher.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from backend.chunker import EMOTIONS, NEUTRAL


@dataclass(frozen=True)
class VoiceParams:
    """What to put on an `audio_query` for one emotion."""

    style_id: int
    speed: float = 1.0
    pitch: float = 0.0
    intonation: float = 1.0
    style_name: str = ""

    def apply(self, query: dict[str, Any], base_speed: float, base_intonation: float,
              pre_phoneme: float | None = None, post_phoneme: float | None = None,
              pause_scale: float | None = None, base_pitch: float = 0.0) -> dict[str, Any]:
        """Return a copy of the query with this emotion's parameters applied.

        `speed`/`intonation` are multipliers on the student's configured baseline, so lowering
        the global speed for a learner slows every emotion with it.

        The phoneme paddings matter more than they look: VOICEVOX defaults to 0.1 s of silence
        at each end, and because we synthesise sentence by sentence (ADR-008) that becomes 0.2 s
        of dead air BETWEEN every sentence — measured, and the main source of the chopped,
        synthetic feel. The player streams continuously, so no lead-in is needed at all.
        """
        out = dict(query)
        out["speedScale"] = round(base_speed * self.speed, 4)
        # Emotion pitch is an OFFSET from the baseline, so lowering the register shifts every
        # emotion with it and the voice stays one person.
        out["pitchScale"] = round(base_pitch + self.pitch, 4)
        out["intonationScale"] = round(base_intonation * self.intonation, 4)
        if pre_phoneme is not None:
            out["prePhonemeLength"] = round(pre_phoneme, 4)
        if post_phoneme is not None:
            out["postPhonemeLength"] = round(post_phoneme, 4)
        if pause_scale is not None and "pauseLengthScale" in out:
            out["pauseLengthScale"] = round(pause_scale, 4)
        return out


#: emotion -> (preferred style names, speed x, pitch +, intonation x). Tuned by ear at M3.
DEFAULT_TABLE: dict[str, tuple[tuple[str, ...], float, float, float]] = {
    NEUTRAL:     ((),                          1.00,  0.00, 1.00),
    "happy":     (("読み聞かせ", "明るい", "喜び", "うきうき", "あまあま"), 1.05,  0.02, 1.15),
    "thinking":  (("おちつき", "ノーマル"),      0.95, -0.01, 0.90),
    "surprised": (("おどろき", "ノーマル"),      1.10,  0.04, 1.30),
    "serious":   (("アナウンス", "シリアス", "つよつよ", "ノーマル"), 0.95, -0.03, 0.85),
}


def styles_for_speaker(speakers: Iterable[dict[str, Any]], style_id: int) -> dict[str, int]:
    """All style names -> ids belonging to the character that owns `style_id`."""
    for speaker in speakers or ():
        styles = speaker.get("styles") or []
        if any(int(s.get("id", -1)) == style_id for s in styles):
            return {str(s.get("name")): int(s["id"]) for s in styles if "id" in s}
    return {}


def _parse_override(raw: str) -> dict[str, str]:
    """`"style=31,speed=1.05,pitch=0.02,intonation=1.15"` -> dict. Blank fields are ignored."""
    out: dict[str, str] = {}
    for part in str(raw or "").split(","):
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if key and value:
            out[key] = value
    return out


def _number(raw: dict[str, str], key: str, fallback: float) -> float:
    try:
        return float(raw[key])
    except (KeyError, ValueError):
        return fallback


#: When a speaker offers no distinct styles, every emotion lands on the same voice and only the
#: scalars separate them — so the scalars have to work harder. Widening the pitch and intonation
#: deltas keeps the emotions audible instead of collapsing into one flat delivery.
SINGLE_STYLE_SPREAD = 1.8


def resolve(cfg, speakers: Iterable[dict[str, Any]] | None = None) -> tuple[dict[str, VoiceParams], list[str]]:
    """Build the emotion -> VoiceParams table. Returns (table, warnings)."""
    base_id = int(cfg.VOICEVOX_SPEAKER)
    if base_id < 0:
        from backend import prompt as prompt_mod
        base_id = prompt_mod.declared_voice(getattr(cfg, "TUTOR_PERSONA", None)) or 29
    available = styles_for_speaker(speakers or (), base_id)
    by_id = {v: k for k, v in available.items()}
    warnings: list[str] = []
    table: dict[str, VoiceParams] = {}

    for emotion in (NEUTRAL, *EMOTIONS):
        names, speed, pitch, intonation = DEFAULT_TABLE.get(emotion, ((), 1.0, 0.0, 1.0))
        override = _parse_override(getattr(cfg, f"EMOTION_{emotion.upper()}", "")) if emotion else {}
        speed = _number(override, "speed", speed)
        pitch = _number(override, "pitch", pitch)
        intonation = _number(override, "intonation", intonation)

        style_id, style_name = base_id, by_id.get(base_id, "")
        wanted = override.get("style")
        if wanted:
            if wanted.isdigit():
                style_id, style_name = int(wanted), by_id.get(int(wanted), "")
            elif wanted in available:
                style_id, style_name = available[wanted], wanted
            else:
                warnings.append(f"{emotion or 'neutral'}: style {wanted!r} not available for speaker {base_id}")
        elif names and available:
            for candidate in names:
                if candidate in available:
                    style_id, style_name = available[candidate], candidate
                    break
        table[emotion] = VoiceParams(style_id, speed, pitch, intonation, style_name)

    # A speaker with one style (麒ヶ島宗麟, 春日部つむぎ, ...) cannot express emotion by switching
    # voice, so give the scalars more room. An explicit EMOTION_* override is left exactly as the
    # user wrote it.
    if len({p.style_id for p in table.values()}) == 1:
        for emotion, params in table.items():
            if emotion == NEUTRAL or _parse_override(getattr(cfg, f"EMOTION_{emotion.upper()}", "")):
                continue
            table[emotion] = VoiceParams(
                params.style_id,
                params.speed,
                round(params.pitch * SINGLE_STYLE_SPREAD, 4),
                round(1.0 + (params.intonation - 1.0) * SINGLE_STYLE_SPREAD, 4),
                params.style_name,
            )

    return table, warnings
