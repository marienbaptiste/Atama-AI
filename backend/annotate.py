"""Furigana for the chat (spec §8b, ADR-036): readings from a local tokenizer, and whether the
student has passed each kanji on WaniKani. No model call and no network — fugashi + unidic-lite
(verified 2026-09-12, constants.py) and the WaniKani snapshot already on disk.

A reading covers one run of kanji — 取り消す gives 取=と and 消=け — so the page can put furigana
over exactly the characters it belongs to, and hide it where the student knows every kanji.
The tokenizer's known misreadings in lesson Japanese (私 as わたくし, 日本 as にっぽん, 明日 as あす)
are corrected from backend/data/readings.txt, matched on the text before it is tokenised.

A run the student half knows (日本語 with 語 still unknown) is split one kanji at a time with
WaniKani's own readings for those kanji (`kanji_readings`, from the snapshot), rendaku and sokuon
allowed (学+校 = がく+こう = がっこう), so the furigana is hidden over 日本 and shown over 語 alone
(user, 2026-09-12). When no split fits, the whole run keeps one reading as before.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

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
#: Bunpro writes a gap in a pattern as ～ (あまり～ない): anything may sit between the two halves.
_GAP = frozenset("〜～~")
#: A point whose name is one short kana token (ば, なら, かな) matches too much of any sentence to
#: be found by the tokenizer alone; her own mark is the only way those go red (user, 2026-09-12:
#: と思います was not red — that one has two tokens and a kanji, so it is found).
_MIN_POINT_TOKENS = 2
#: Auxiliaries (ます, た, ない, て…) that finish the inflected form: the red span runs through
#: them, as the tutor is told to mark stem to ending.
_TAIL_POS = ("助動詞",)


#: Rendaku: the voiced form a kanji's reading takes after another (本 ほん -> 日本 にっぽん is
#: sokuon + は->ぱ; 花 はな -> 生け花 いけばな). Only the first kana of a non-initial kanji.
_VOICED = {"か": "が", "き": "ぎ", "く": "ぐ", "け": "げ", "こ": "ご",
           "さ": "ざ", "し": "じ", "す": "ず", "せ": "ぜ", "そ": "ぞ",
           "た": "だ", "ち": "ぢ", "つ": "づ", "て": "で", "と": "ど",
           "は": "ばぱ", "ひ": "びぴ", "ふ": "ぶぷ", "へ": "べぺ", "ほ": "ぼぽ"}
#: Sokuon: a reading's last kana that becomes っ before the next kanji (学校 がく+こう -> がっこう).
_SOKUON = "つくきち"
#: A per-kanji reading in readings.txt: 日本語 に.ほん.ご (one piece per kanji, in order).
_PIECE = "."


def to_hiragana(text: str) -> str:
    """Katakana to hiragana (the tokenizer reads in katakana); anything else unchanged."""
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def kana_table(readings: Mapping[str, Iterable[str]] | None) -> dict[str, list[str]]:
    """A kanji -> readings table in hiragana, duplicates dropped, order kept (WaniKani's onyomi may
    arrive in katakana; the snapshot is stdlib-only, so the conversion lives here)."""
    out: dict[str, list[str]] = {}
    for char, options in (readings or {}).items():
        seen: list[str] = []
        for r in options:
            r = to_hiragana(str(r or "")).strip()
            if r and r not in seen:
                seen.append(r)
        if char and seen:
            out[str(char)] = seen
    return out


def _variants(base: str, first: bool, last: bool) -> list[str]:
    """How `base` may sound inside a compound: as is; voiced when it follows another kanji; with
    its last kana as っ when another kanji follows; both. Longest first, so backtracking prefers
    the fuller match."""
    forms = [base]
    if not first and base and base[0] in _VOICED:
        forms += [v + base[1:] for v in _VOICED[base[0]]]
    if not last and len(base) > 1 and base[-1] in _SOKUON:
        forms += [f[:-1] + "っ" for f in forms]
    return sorted(dict.fromkeys(forms), key=len, reverse=True)


def split_reading(run: str, reading: str, table: Mapping[str, Iterable[str]]) -> list[str] | None:
    """`reading` cut into one piece per character of the kanji run `run`, each piece one of that
    kanji's readings in `table` (rendaku and sokuon allowed; 々 repeats the kanji before it) —
    or None when no full split exists. 学校/がっこう with 学=[がく] 校=[こう] -> [がっ, こう]."""
    chars = list(run)
    if len(chars) < 2 or not reading:
        return None

    def options(i: int) -> list[str]:
        char = chars[i]
        if char in _MARKS:
            return options(i - 1) if i else []
        return [str(r) for r in table.get(char, ())]

    def rec(i: int, pos: int) -> list[str] | None:
        if i == len(chars):
            return [] if pos == len(reading) else None
        for base in options(i):
            for form in _variants(base, i == 0, i == len(chars) - 1):
                if reading.startswith(form, pos):
                    rest = rec(i + 1, pos + len(form))
                    if rest is not None:
                        return [form] + rest
        return None

    return rec(0, 0)


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
    """`word  reading` per line, `#` comments. A missing file is no corrections, not an error.
    A reading may be dotted per kanji (日本語 に.ほん.ご): kept as written, read by `_pieces`."""
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
    def __init__(self, known_kanji: Iterable[str] = (), overrides: dict[str, str] | None = None,
                 kanji_readings: Mapping[str, Iterable[str]] | None = None) -> None:
        #: Kanji the student has passed (reached Guru on) in WaniKani. Refreshed with the profile.
        self.known: set[str] = set(known_kanji)
        self.overrides = load_overrides() if overrides is None else overrides
        #: kanji -> its readings on WaniKani (`WaniKaniProfile.kanji_readings`), in hiragana here,
        #: so a half-known run can be split one kanji at a time. Refreshed with the profile.
        self.kanji_readings = kanji_readings
        self.error = ""
        self._tagger: Any = None
        self._lock = threading.Lock()

    @property
    def kanji_readings(self) -> dict[str, list[str]]:
        return self._kanji_readings

    @kanji_readings.setter
    def kanji_readings(self, readings: Mapping[str, Iterable[str]] | None) -> None:
        self._kanji_readings = kana_table(readings)

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

    def find_points(self, text: str, points: Iterable[str],
                    taken: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
        """Grammar points from the student's own list that appear in `text` without a mark from
        the tutor: [{start, end, point}] over code points, none overlapping `taken` (her marks).

        The tutor is told to wrap every point she uses, and forgets: 「と思います」 stayed black on
        the page although 「と思う」 was in the student's unsettled list (user, 2026-09-12). So the
        tokenizer looks for each point by BASE FORMS — 思います and 思っ both read 思う — which
        catches every inflection without a table of them. A gap in the point's name (あまり～ない)
        may hold anything. Conservative on purpose: a point that is one short token (ば, なら) is
        never guessed, the whole inflected form is covered (through ます, た, ない), and the tutor's
        own marks always win. Never raises: without a tokenizer there is simply no safety net."""
        try:
            return self._find_points(text, points, taken)
        except Exception:  # noqa: BLE001 - a colour is never worth a lost sentence
            return []

    def tokens(self, text: str) -> list[tuple[int, int, str, str, str]]:
        """The tokenizer's view of `text`: (start, end, orthBase or surface, lemma or surface,
        pos1) per token, positions over code points. For matching the student's vocabulary by
        whole token rather than substring (backend/study.py): 申す must never match the もう of
        もう一度, while たけ still resolves to 竹 through the lemma (user, 2026-09-12). Never
        raises; empty without a tokenizer."""
        try:
            tagger = self._get()
            if tagger is None or not text:
                return []
            out, pos = [], 0
            for w in tagger(text):
                start = text.find(w.surface, pos)
                if start < 0:
                    continue
                pos = start + len(w.surface)
                base = getattr(w.feature, "orthBase", None) or w.surface
                lemma = getattr(w.feature, "lemma", None) or w.surface
                out.append((start, pos, str(base), str(lemma), str(getattr(w.feature, "pos1", "") or "")))
            return out
        except Exception:  # noqa: BLE001 - a match is never worth a lost sentence
            return []

    # ----------------------------------------------------------------- private
    def _find_points(self, text: str, points: Iterable[str], taken: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        tagger = self._get()
        if tagger is None or not text:
            return []
        words = self._tokens(tagger, text)
        if not words:
            return []
        covered = [False] * len(text)
        for m in taken:
            covered[int(m["start"]):int(m["end"])] = [True] * max(0, int(m["end"]) - int(m["start"]))
        out: list[dict[str, Any]] = []
        for point in points:
            halves = self._point_halves(tagger, point)
            if not halves:
                continue
            for start, end in self._matches(words, halves):
                s, e = words[start][0], words[end][1]
                while end + 1 < len(words) and words[end + 1][3] in _TAIL_POS:   # through ます, た, ない
                    end += 1
                    e = words[end][1]
                if any(covered[s:e]):
                    continue
                covered[s:e] = [True] * (e - s)
                out.append({"start": s, "end": e, "point": point})
        return sorted(out, key=lambda r: r["start"])

    @staticmethod
    def _tokens(tagger: Any, text: str) -> list[tuple[int, int, str, str]]:
        """(start, end, base form, pos1) per token, positions over the text's code points."""
        out, pos = [], 0
        for w in tagger(text):
            start = text.find(w.surface, pos)
            if start < 0:
                continue
            pos = start + len(w.surface)
            base = getattr(w.feature, "orthBase", None) or getattr(w.feature, "lemma", None) or w.surface
            out.append((start, pos, str(base), str(getattr(w.feature, "pos1", "") or "")))
        return out

    @staticmethod
    def _point_halves(tagger: Any, point: str) -> list[list[str]]:
        """The point's name as base-form token runs, split at its gaps: と思う -> [[と, 思う]],
        あまり～ない -> [[あまり], [ない]]. Empty when the name is not one the tokenizer can find."""
        name = str(point or "").strip()
        if not name or "[" in name or "(" in name:              # "Verb[よう]", "する (Have/Wear)"
            return []
        halves: list[list[str]] = [[]]
        for w in tagger(name):
            if w.surface in _GAP:
                if halves[-1]:
                    halves.append([])
                continue
            base = getattr(w.feature, "orthBase", None) or getattr(w.feature, "lemma", None) or w.surface
            halves[-1].append(str(base))
        halves = [h for h in halves if h]
        tokens = sum(len(h) for h in halves)
        if not halves or (tokens < _MIN_POINT_TOKENS and not _KANJI.search(name)):
            return []
        return halves

    @staticmethod
    def _matches(words: list[tuple[int, int, str, str]], halves: list[list[str]]) -> list[tuple[int, int]]:
        """Token index ranges [first, last] where the halves appear in order, base form for base
        form, with any tokens between two halves."""
        bases = [w[2] for w in words]

        def run_at(i: int, half: list[str]) -> bool:
            return bases[i:i + len(half)] == half

        found: list[tuple[int, int]] = []
        i = 0
        while i < len(bases):
            if not run_at(i, halves[0]):
                i += 1
                continue
            last = i + len(halves[0]) - 1
            ok = True
            for half in halves[1:]:
                j = last + 1
                while j < len(bases) and not run_at(j, half):
                    j += 1
                if j >= len(bases):
                    ok = False
                    break
                last = j + len(half) - 1
            if ok:
                found.append((i, last))
                i = last + 1
            else:
                i += 1
        return found

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
            reading, pieces = self._pieces(word, self.overrides[word])
            for m in re.finditer(re.escape(word), text):
                if any(covered[m.start():m.end()]):
                    continue
                for s, e, r in align(word, reading):
                    out += self._entries(text, m.start() + s, m.start() + e, r, pieces)
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
                for s, e, r in align(word.surface, to_hiragana(kana)):
                    out += self._entries(text, start + s, start + e, r)
        return sorted(out, key=lambda r: r["start"])

    @staticmethod
    def _pieces(word: str, reading: str) -> tuple[str, dict[str, list[str]]]:
        """A correction's reading, and — when it is dotted per kanji (に.ほん.ご) — a table of
        those pieces by kanji, so the run can be split without WaniKani's help."""
        if _PIECE not in reading:
            return reading, {}
        pieces = reading.split(_PIECE)
        kanji = [c for c in word if _KANJI.match(c)]
        if len(pieces) != len(kanji):
            return reading.replace(_PIECE, ""), {}
        table: dict[str, list[str]] = {}
        for char, piece in zip(kanji, pieces):
            table.setdefault(char, []).append(piece)
        return "".join(pieces), table

    def _entries(self, text: str, start: int, end: int, reading: str,
                 pieces: Mapping[str, list[str]] | None = None) -> list[dict[str, Any]]:
        """One entry for the run — or one per kanji when the student knows some of it and not the
        rest, and the reading splits along their WaniKani readings."""
        run = text[start:end]
        kanji = [c for c in run if _KANJI.match(c) and c not in _MARKS]
        known = [c in self.known for c in kanji]
        if len(run) >= 2 and any(known) and not all(known):
            table = {**self.kanji_readings, **(pieces or {})}
            split = split_reading(run, reading, table)
            if split is not None:
                return [self._entry(text, start + i, start + i + 1, piece)
                        for i, piece in enumerate(split)]
        return [self._entry(text, start, end, reading)]

    def _entry(self, text: str, start: int, end: int, reading: str) -> dict[str, Any]:
        kanji = [c for c in text[start:end] if _KANJI.match(c) and c not in _MARKS]
        return {"start": start, "end": end, "reading": reading,
                "known": bool(kanji) and all(c in self.known for c in kanji)}
