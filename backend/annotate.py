"""Furigana for the chat (spec §8b, ADR-036): readings from a local tokenizer, and whether the
student has passed each kanji on WaniKani. No model call and no network — fugashi + unidic-lite
(verified 2026-09-12, constants.py) and the WaniKani snapshot already on disk.

A reading covers one run of kanji — 取り消す gives 取=と and 消=け — so the page can put furigana
over exactly the characters it belongs to, and hide it where the student knows every kanji.
The tokenizer's known misreadings in lesson Japanese (私 as わたくし, 日本 as にっぽん, 明日 as あす)
are corrected from backend/data/readings.txt, matched on the text before it is tokenised.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Iterable

from backend import config

OVERRIDES_FILE = config.REPO_ROOT / "backend" / "data" / "readings.txt"
#: CJK ideographs (the BMP blocks, the compatibility block, extensions B+) plus 々 and 〆.
_KANJI = re.compile(r"[々〆㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f]")
_KANJI_RUN = re.compile(_KANJI.pattern + "+")
#: Iteration marks repeat the kanji before them; they are never "a kanji you know" on their own.
_MARKS = "々〆"


def to_hiragana(text: str) -> str:
    """Katakana to hiragana (the tokenizer reads in katakana); anything else unchanged."""
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def align(surface: str, reading: str) -> list[tuple[int, int, str]]:
    """Each kanji run of `surface` with its share of `reading` (hiragana): 取り消す/とりけす →
    [(0, 1, "と"), (2, 3, "け")]. When the kana do not line up, one reading goes over the span from
    the first kanji to the last, minus the kana the word starts and ends with."""
    runs = list(_KANJI_RUN.finditer(surface))
    if not runs or not reading:
        return []
    pattern, pos = "", 0
    for m in runs:
        pattern += re.escape(to_hiragana(surface[pos:m.start()])) + "(.+?)"
        pos = m.end()
    pattern += re.escape(to_hiragana(surface[pos:]))
    if hit := re.fullmatch(pattern, reading):
        return [(m.start(), m.end(), hit.group(i + 1)) for i, m in enumerate(runs)]
    start, end = runs[0].start(), runs[-1].end()
    lead, tail = to_hiragana(surface[:start]), to_hiragana(surface[end:])
    core = reading
    if reading.startswith(lead) and reading.endswith(tail) and len(reading) > len(lead) + len(tail):
        core = reading[len(lead):len(reading) - len(tail)]
    return [(start, end, core)]


def load_overrides(path: Path = OVERRIDES_FILE) -> dict[str, str]:
    """`word  reading` per line, `#` comments. A missing file is no corrections, not an error."""
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


class Annotator:
    def __init__(self, known_kanji: Iterable[str] = (), overrides: dict[str, str] | None = None) -> None:
        #: Kanji the student has passed (reached Guru on) in WaniKani. Refreshed with the profile.
        self.known: set[str] = set(known_kanji)
        self.overrides = load_overrides() if overrides is None else overrides
        self.error = ""
        self._tagger: Any = None
        self._lock = threading.Lock()

    def warm(self) -> bool:
        """Load the tokenizer now (~0.3 s, measured 2026-09-12) rather than on her first sentence."""
        return self._get() is not None

    def readings(self, text: str) -> list[dict[str, Any]]:
        """Readings for the kanji runs in `text`: [{start, end, reading, known}] over code points.
        Never raises — without a tokenizer there is simply less furigana, never a lost sentence."""
        if not text or not _KANJI.search(text):
            return []
        try:
            return self._readings(text)
        except Exception:  # noqa: BLE001 - furigana is a nicety; the sentence matters more
            return []

    # ----------------------------------------------------------------- private
    def _get(self) -> Any:
        with self._lock:
            if self._tagger is None and not self.error:
                try:
                    from fugashi import Tagger
                    self._tagger = Tagger()
                except Exception as exc:  # noqa: BLE001 - not installed, or no dictionary
                    self.error = f"{type(exc).__name__}: {exc}"
            return self._tagger

    def _readings(self, text: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        covered = [False] * len(text)
        for word in sorted(self.overrides, key=len, reverse=True):      # longest correction first
            for m in re.finditer(re.escape(word), text):
                if any(covered[m.start():m.end()]):
                    continue
                out += [self._entry(text, m.start() + s, m.start() + e, r) for s, e, r in align(word, self.overrides[word])]
                covered[m.start():m.end()] = [True] * (m.end() - m.start())
        tagger = self._get()
        if tagger is not None:
            pos = 0
            for word in tagger(text):
                start = text.find(word.surface, pos)
                if start < 0:
                    continue
                pos = start + len(word.surface)
                kana = getattr(word.feature, "kana", None)
                if any(covered[start:pos]) or not kana or not _KANJI.search(word.surface):
                    continue
                out += [self._entry(text, start + s, start + e, r) for s, e, r in align(word.surface, to_hiragana(kana))]
        return sorted(out, key=lambda r: r["start"])

    def _entry(self, text: str, start: int, end: int, reading: str) -> dict[str, Any]:
        kanji = [c for c in text[start:end] if _KANJI.match(c) and c not in _MARKS]
        return {"start": start, "end": end, "reading": reading,
                "known": bool(kanji) and all(c in self.known for c in kanji)}
