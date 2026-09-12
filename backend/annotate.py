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

#: Red in the chat is grammar (the user, 2026-09-12), so a mark whose every token is one of these
#: — a noun, a counter or suffix on it, an interjection — is a word the tutor liked, not a point
#: worth teaching. Deliberately narrow: verbs, adjectives, adverbs, pronouns and 連体詞 stay out,
#: because 「できる」「ない」「あまり」「これ」「この」 are single tokens AND real Bunpro points.
_WORD_POS = ("名詞", "接尾辞", "感動詞")
#: Nouns that only ever appear bound to a clause. unidic tags them 名詞,普通名詞 like any other
#: noun, but 「つもり」 on its own IS the grammar point, so the guard leaves them alone. Kana only:
#: 中, 方, 間, 際 are words as often as patterns, and the point's own name separates those.
_BOUND_NOUNS = frozenset("こと もの つもり はず ため わけ ところ とおり うち あいだ ほう まま "
                         "ごろ ばかり くらい ぐらい おかげ せい たび かわり".split())
#: How Bunpro names a pattern, and what the tutor is told to copy: a point named 〜中 is a claim
#: that this is a form, not a word, and the guard takes the tutor at its word.
_PATTERN_NAME = ("〜", "～", "~", "ー")


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

    def grammar_only(self, text: str, marks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The marks of `text` that are really grammar: red is for grammar, so a span that is one
        plain word is dropped before the page ever sees it.

        The tutor is told this in `prompts/tutor.md`, but a word it finds useful still slips
        through, so the tokenizer has the last word. Conservative on purpose — nothing but nouns in
        the span, the point not named as a pattern, no bound noun — because dropping a real point
        costs more than leaving a stray word red. Never raises: with no tokenizer, every mark
        stands."""
        if not marks:
            return marks
        try:
            return [m for m in marks
                    if not self._is_word(text[int(m["start"]):int(m["end"])], str(m.get("point", "")))]
        except Exception:  # noqa: BLE001 - the guard is a nicety; the marks matter more
            return marks

    # ----------------------------------------------------------------- private
    def _is_word(self, span: str, point: str) -> bool:
        """Vocabulary, not grammar: every token a noun-ish one, and nothing claiming to be a form."""
        span, point = span.strip(), point.strip()
        tagger = self._get()
        if tagger is None or not span or point.startswith(_PATTERN_NAME) or span in _BOUND_NOUNS:
            return False
        words = list(tagger(span))
        kinds = [(getattr(w.feature, "pos1", "") or "") for w in words]
        return (bool(words) and "名詞" in kinds and all(k in _WORD_POS for k in kinds)
                and not any(w.surface in _BOUND_NOUNS for w in words))


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
