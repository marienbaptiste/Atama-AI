"""Sentence chunker (spec §2 step 5, §4, ADR-008, ADR-020).

Turns Claude's streamed text deltas into speakable sentences, each carrying the emotion that
applies to it. Pure: no I/O, no clock, no state beyond one turn's buffer.

Rules:
- Split at 。！？…\\n as deltas arrive (spec §2) — never wait for the whole reply. ASCII `!?` too,
  for the English-explanation mode; NOT `.`, which is ambiguous (decimals, abbreviations), so an
  English sentence splits on `!`, `?` or a newline.
- Never split mid-sentence when a delta lands across a boundary character, and never emit a
  chunk that has nothing speakable in it (a lone 「…」, a bare newline).
- Strip exactly one emotion tag `[happy]|[thinking]|[surprised]|[serious]` at the start of the
  turn or of any sentence; it applies to that sentence and the following ones until the next tag
  (ADR-020). A tag split across two deltas (`[hap` + `py]`) is still recognised.
- A tag the model puts MID-sentence violates the prompt ("Nothing else in brackets, ever"). It is
  removed from the spoken text — a bracket must never reach TTS — applied from the NEXT sentence,
  and recorded in `stray_tags` so a prompt bug is visible rather than silent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

EMOTIONS = ("happy", "thinking", "surprised", "serious")
NEUTRAL = ""
TERMINATORS = "。！？!?…‥\n"
#: A chunk must contain at least one of these to be worth synthesising.
_SPEAKABLE = re.compile(r"[^\s。、！？!?…‥・「」『』（）()\[\]【】〜~—\-—.,]")
_TAG_AT_START = re.compile(r"^\s*\[(" + "|".join(EMOTIONS) + r")\]\s*")
_ANY_TAG = re.compile(r"\[(" + "|".join(EMOTIONS) + r")\]\s*")
#: Longest prefix that could still become a tag once more deltas arrive, e.g. "[hap".
_PARTIAL_TAG = re.compile(r"^\s*\[[a-z]*$")


@dataclass(frozen=True)
class Chunk:
    """One speakable sentence plus the emotion in force for it."""

    text: str
    emotion: str = NEUTRAL


@dataclass
class SentenceChunker:
    """Feed `push(delta)`, read the returned chunks; `close()` flushes the tail.

    One instance per assistant turn. Emotion carries across sentences within the turn.
    """

    _buf: str = ""
    _emotion: str = NEUTRAL
    _pending: str | None = None
    stray_tags: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ public
    def push(self, delta: str) -> list[Chunk]:
        """Add a text delta; return every sentence that closed because of it."""
        if not delta:
            return []
        self._buf += delta
        out: list[Chunk] = []
        while (chunk := self._take_sentence()) is not None:
            if chunk.text:
                out.append(chunk)
        return out

    def close(self) -> list[Chunk]:
        """End of turn: emit whatever is left, even without a terminator."""
        self._consume_leading_tag(final=True)
        raw, self._buf = self._buf, ""
        chunk = self._build(raw)
        return [chunk] if chunk.text else []

    @property
    def emotion(self) -> str:
        """Emotion in force after the last emitted chunk."""
        return self._emotion

    # ----------------------------------------------------------------- private
    def _take_sentence(self) -> Chunk | None:
        """Pop one complete sentence, or None if the buffer holds no closed sentence yet."""
        self._consume_leading_tag()
        if _PARTIAL_TAG.match(self._buf):
            return None  # wait: this may still become a tag
        idx = next((i for i, ch in enumerate(self._buf) if ch in TERMINATORS), -1)
        if idx == -1:
            return None
        # Absorb a run of terminators so 「…。」 or 「。\n」 do not leave a punctuation-only tail.
        end = idx + 1
        while end < len(self._buf) and self._buf[end] in TERMINATORS:
            end += 1
        raw, self._buf = self._buf[:end], self._buf[end:]
        return self._build(raw)

    def _build(self, raw: str) -> Chunk:
        """Clean one sentence: drop stray tags, apply the pending emotion, drop the unspeakable."""
        text = raw.strip()
        if strays := _ANY_TAG.findall(text):
            self.stray_tags.extend(strays)
            text = _ANY_TAG.sub("", text).strip()
            # A mid-sentence tag is malformed: honour the intent from the NEXT sentence.
            emotion = self._apply_pending()
            self._pending = strays[-1]
            return Chunk(text if _SPEAKABLE.search(text) else "", emotion)
        if not _SPEAKABLE.search(text):
            return Chunk("", self._emotion)
        return Chunk(text, self._apply_pending())

    def _consume_leading_tag(self, final: bool = False) -> None:
        """Strip tags at the head of the buffer (there may be more than one if the model repeats)."""
        while (m := _TAG_AT_START.match(self._buf)) is not None:
            self._pending = m.group(1)
            self._buf = self._buf[m.end() :]
        if final and _PARTIAL_TAG.match(self._buf):
            self._buf = ""  # an unterminated "[hap" at end of turn is not speech

    def _apply_pending(self) -> str:
        if self._pending is not None:
            self._emotion, self._pending = self._pending, None
        return self._emotion


def strip_tag(text: str) -> tuple[str, str]:
    """Convenience for non-streaming callers: (text without a leading tag, emotion)."""
    m = _TAG_AT_START.match(text)
    return (text[m.end() :], m.group(1)) if m else (text, NEUTRAL)
