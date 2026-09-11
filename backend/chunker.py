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
- Study marks (ADR-036, spec §8b) are shown on the page and NEVER spoken: `{{span|point}}` wraps a
  grammar use and becomes `span` plus a `GrammarMark` on the cleaned text; `[target:point]` names
  what she wants the student to use next and rides on the sentence it opens (or sits in). Whatever
  is left of a malformed mark is removed and recorded in `stray_marks`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The tags the tutor may emit. Deliberately NOT TalkingHead's full mood set: `angry`,
#: `disgust`, `fear` and `sleep` exist in the library and are not offered here, because a tag the
#: model can reach for is a tag it will eventually use, and none of those belong on a teacher.
#: `encouraging`, `proud` and `confused` earn their place — they are the three things a tutor
#: does constantly that the original four could not express (2026-09-10).
EMOTIONS = ("happy", "thinking", "surprised", "serious", "encouraging", "proud", "confused")
NEUTRAL = ""
TERMINATORS = "。！？!?…‥\n"
#: A chunk must contain at least one of these to be worth synthesising.
_SPEAKABLE = re.compile(r"[^\s。、！？!?…‥・「」『』（）()\[\]【】〜~—\-—.,]")
_TAG_AT_START = re.compile(r"^\s*\[(" + "|".join(EMOTIONS) + r")\]\s*")
_ANY_TAG = re.compile(r"\[(" + "|".join(EMOTIONS) + r")\]\s*")
#: Longest prefix that could still become a tag once more deltas arrive, e.g. "[hap" or
#: "[target:〜た" — bounded, so an unclosed target never swallows the rest of the reply.
_PARTIAL_TAG = re.compile(r"^\s*\[(?:[a-z]*|(?:target|used):[^\]\n。！？!?]{0,40})$")
_GRAMMAR = re.compile(r"\{\{([^{}|\n]+)\|([^{}\n]+)\}\}")
_TARGET = re.compile(r"\[target:([^\]\n]*)\]\s*")
_TARGET_AT_START = re.compile(r"^\s*\[target:([^\]\n]*)\]\s*")
#: The student just used this correctly — the page floats it behind her (user, 2026-09-12).
_USED = re.compile(r"\[used:([^\]\n]*)\]\s*")
_USED_AT_START = re.compile(r"^\s*\[used:([^\]\n]*)\]\s*")
#: What a malformed mark leaves behind: a "|point}}" tail, a lone "{{" or "}}", an unclosed tag.
_BROKEN = re.compile(r"\|[^{}|\n]*\}\}|\{\{|\}\}|\[(?:target|used):")


@dataclass(frozen=True)
class GrammarMark:
    """Where she used a grammar point: characters [start, end) of the cleaned sentence, counted in
    code points (as Python indexes str), and the point's name."""

    start: int
    end: int
    point: str


@dataclass(frozen=True)
class Chunk:
    """One speakable sentence plus the emotion in force for it."""

    text: str
    emotion: str = NEUTRAL
    #: Study marks (ADR-036): her grammar uses in this sentence, and what she wants the student
    #: to use next if this sentence asks for it.
    grammar: tuple[GrammarMark, ...] = ()
    target: str = ""
    #: A word or grammar point the student has just used correctly, for the page to celebrate.
    used: str = ""

    def as_log(self) -> dict:
        """This sentence as the turn log records it (spec §6b); study marks only when present."""
        out: dict = {"text": self.text, "emotion": self.emotion or None, "synth_ms": None}
        if self.grammar:
            out["grammar"] = [{"span": self.text[g.start:g.end], "point": g.point} for g in self.grammar]
        if self.target:
            out["target"] = self.target
        if self.used:
            out["used"] = self.used
        return out


@dataclass
class SentenceChunker:
    """Feed `push(delta)`, read the returned chunks; `close()` flushes the tail.

    One instance per assistant turn. Emotion carries across sentences within the turn.
    """

    _buf: str = ""
    _emotion: str = NEUTRAL
    _pending: str | None = None
    #: A target, and a "the student used this" mark, seen and not yet attached to a spoken sentence.
    _target: str = ""
    _used: str = ""
    stray_tags: list[str] = field(default_factory=list)
    stray_marks: list[str] = field(default_factory=list)

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
        """Clean one sentence: drop stray tags, apply the pending emotion, drop the unspeakable,
        and turn study marks into spans on the cleaned text."""
        text = raw.strip()
        for pattern, field_name in ((_TARGET, "_target"), (_USED, "_used")):
            for m in pattern.finditer(text):    # a mark past the head: still never spoken
                if m.group(1).strip():
                    setattr(self, field_name, m.group(1).strip())
            text = pattern.sub("", text).strip()
        emotion: str | None = None
        if strays := _ANY_TAG.findall(text):
            self.stray_tags.extend(strays)
            text = _ANY_TAG.sub("", text).strip()
            # A mid-sentence tag is malformed: honour the intent from the NEXT sentence.
            emotion = self._apply_pending()
            self._pending = strays[-1]
        text, grammar = self._marks(text)
        if not _SPEAKABLE.search(text):
            return Chunk("", emotion if emotion is not None else self._emotion)
        target, self._target = self._target, ""
        used, self._used = self._used, ""
        return Chunk(text, emotion if emotion is not None else self._apply_pending(), grammar, target, used)

    def _marks(self, text: str) -> tuple[str, tuple[GrammarMark, ...]]:
        """`{{span|point}}` becomes `span`, and a mark saying where it sits in the cleaned text."""
        parts: list[str] = []
        marks: list[GrammarMark] = []
        size = last = 0
        for m in _GRAMMAR.finditer(text):
            before = self._unbroken(text[last:m.start()])
            span = m.group(1)
            parts += [before, span]
            marks.append(GrammarMark(size + len(before), size + len(before) + len(span), m.group(2).strip()))
            size += len(before) + len(span)
            last = m.end()
        parts.append(self._unbroken(text[last:]))
        joined = "".join(parts)
        lead = len(joined) - len(joined.lstrip())
        clean = joined.strip()
        return clean, tuple(GrammarMark(g.start - lead, g.end - lead, g.point) for g in marks
                            if g.point and 0 <= g.start - lead < g.end - lead <= len(clean))

    def _unbroken(self, segment: str) -> str:
        if _BROKEN.search(segment):
            self.stray_marks.append(segment.strip()[:40])
            segment = _BROKEN.sub("", segment)
        return segment

    def _consume_leading_tag(self, final: bool = False) -> None:
        """Strip tags at the head of the buffer — emotion tags and targets, in either order, more
        than one if the model repeats."""
        while True:
            if (m := _TAG_AT_START.match(self._buf)) is not None:
                self._pending = m.group(1)
            elif (m := _TARGET_AT_START.match(self._buf)) is not None:
                self._target = m.group(1).strip() or self._target
            elif (m := _USED_AT_START.match(self._buf)) is not None:
                self._used = m.group(1).strip() or self._used
            else:
                break
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
