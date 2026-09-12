"""What the student is still learning, and where it appears in a sentence (spec §8b, ADR-036).

Red is grammar; **blue is a word from their own lessons** (user, 2026-09-12). Which words those
are is already on disk: the WaniKani snapshot fetched at launch (ADR-024 — no new call is made
here, ever) lists every vocabulary item still below Guru — not only the newest thirty, because 13 % of her
sentences carried one of the student's words when that was all she had (measured from the turn
logs, 2026-09-12) — and Bunpro's ghosts and beginner-stage points are the grammar they have not
mastered. "Not yet Guru'd" is stage < 5
(`wanikani.STAGE_BUCKETS`), which is WaniKani's own line between learning and learned.

Matching is plain longest-first string search over the sentence, with no model call and no
network: their vocabulary is written the way WaniKani writes it, and the tutor is told to prefer
exactly those words. A word the student has already Guru'd is not marked — the point of the colour
is "this is one of yours, in play", not "this is a noun".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

#: WaniKani stages 1-4 are Apprentice; 5 is Guru, where an item counts as learned. Same line the
#: kanji furigana uses (`passed_at`, set when stage 5 is first reached — docs read 2026-09-12).
GURU_STAGE = 5
#: Marking every occurrence of a one-character word (人, 日) would paint the whole conversation.
MIN_CHARS = 2
#: A cap, so a huge unlock list cannot slow the per-sentence scan.
MAX_ITEMS = 220
#: Bunpro writes its points as 〜たら, ～ている, 「ので」; the tutor names them the same way. None of
#: that punctuation survives into a comparison.
_TRIM = re.compile(r"[〜～\s「」『』（）()【】・]+")


@dataclass(frozen=True)
class Item:
    """One thing the student is still learning."""

    text: str
    kind: str          # "vocab" | "grammar"
    reading: str = ""
    meaning: str = ""
    #: WaniKani's SRS stage, 1-4 Apprentice, 5-6 Guru. Shown on the word card as its name.
    stage: int = 0
    #: Where it sits in the student's own SRS, for the study plan's weakness order (spec §6c):
    #: a WaniKani leech, and Bunpro's stage name ("ghost" | "beginner" | "adept" | "seasoned").
    leech: bool = False
    srs: str = ""


#: WaniKani's own names for the stages (STAGE_BUCKETS in srs/wanikani.py), for the word card.
_STAGES = {1: "Apprentice 1", 2: "Apprentice 2", 3: "Apprentice 3", 4: "Apprentice 4",
           5: "Guru 1", 6: "Guru 2", 7: "Master", 8: "Enlightened", 9: "Burned"}


def stage_name(stage: int) -> str:
    """"Apprentice 2" for 2, "" for anything that is not a WaniKani stage."""
    return _STAGES.get(int(stage or 0), "")


def normalise(phrase: str) -> str:
    """A grammar point or word reduced to what two spellings of it have in common."""
    return _TRIM.sub("", str(phrase or "")).strip()


#: (start, end, base form, lemma, pos1) per token of a sentence — `Annotator.tokens`. Matching a
#: word by WHOLE tokens is what stops 申す (reading もうす) from painting the もう of もう一度: a
#: substring of a reading is not a word (user, 2026-09-12, screenshot). The tokenizer's base
#: form catches every inflection (聞こえにくかった -> 聞こえる) and its lemma catches a word she
#: wrote in kana (たけ -> 竹), so no hand-made stem or reading form is needed any more.
Tokens = Callable[[str], list[tuple[int, int, str, str, str]]]
#: Longest run of adjacent tokens tried as one word (竹の子, 気に入る when split three ways).
_MAX_JOIN = 3


class Study:
    """The student's own words and grammar points, and where they appear in a sentence."""

    def __init__(self, items: Iterable[Item] = (), tokens: Tokens | None = None) -> None:
        self.items: list[Item] = list(items)[:MAX_ITEMS]
        #: The tokenizer, when there is one. Without it a word is matched only as WaniKani writes
        #: it — the kanji form, never a reading — which misses inflections but invents nothing.
        self.tokens = tokens
        #: A word as WaniKani writes it (and stripped of pattern punctuation), for whole-token
        #: lookups: surface, base form, lemma, or the join of a few adjacent tokens.
        self._words: dict[str, Item] = {}
        for item in self.items:
            if item.kind == "vocab":
                self._words.setdefault(item.text, item)
                self._words.setdefault(normalise(item.text), item)
        self._exact = sorted({i.text for i in self.items if i.kind == "vocab" and len(i.text) >= MIN_CHARS},
                             key=len, reverse=True)
        self._by_key = {normalise(i.text): i for i in self.items}

    @classmethod
    def from_profile(cls, profile: Any, tokens: Tokens | None = None) -> "Study":
        """Built from the snapshot already in memory. Never fetches (ADR-024) and never raises."""
        items: list[Item] = []
        try:
            wk = getattr(profile, "wanikani", None)
            seen: set[str] = set()
            leeches = {str(getattr(v, "characters", "") or "") for v in (getattr(wk, "leeches", []) or [])}
            # in_progress is everything below Guru; the other two are subsets of it on a fresh
            # snapshot and the fallback on an old one.
            for vocab in (list(getattr(wk, "in_progress", []) or [])
                          + list(getattr(wk, "recent_unlocks", []) or [])
                          + list(getattr(wk, "leeches", []) or [])):
                word = str(getattr(vocab, "characters", "") or "")
                if (len(word) < MIN_CHARS or word in seen
                        or int(getattr(vocab, "srs_stage", 0) or 0) >= GURU_STAGE):
                    continue
                seen.add(word)
                items.append(Item(word, "vocab", str(getattr(vocab, "reading", "") or ""),
                                  str(getattr(vocab, "meaning", "") or ""),
                                  int(getattr(vocab, "srs_stage", 0) or 0), leech=word in leeches))
            bp = getattr(profile, "bunpro", None)
            # in_play is everything still in their SRS (ghost, beginner, adept, seasoned); the
            # other two are its head, and the fallback on an older snapshot.
            for point in (list(getattr(bp, "in_play", []) or [])
                          + list(getattr(bp, "ghosts", []) or [])
                          + list(getattr(bp, "weak_grammar", []) or [])):
                title = str(getattr(point, "title", "") or "")
                if title and normalise(title) not in {normalise(i.text) for i in items}:
                    items.append(Item(title, "grammar", meaning=str(getattr(point, "meaning", "") or ""),
                                      srs=str(getattr(point, "srs", "") or "")))
        except Exception:  # noqa: BLE001 - a colour is never worth a lost sentence
            return cls(items, tokens)
        return cls(items, tokens)

    def spans(self, text: str) -> list[dict[str, Any]]:
        """Every word of theirs in `text`: [{start, end, word, ...}] over code points, no overlaps.

        With a tokenizer, a word is one token or a run of adjacent tokens whose surface, base
        form, lemma, or joined base forms IS the word — 「聞こえにくかった」 is 聞こえる, 「勉強します」
        is 勉強する, 「たけ」 is 竹 — and never a piece of a token: もう一度 has no 申す in it. Without
        one, only the spelling WaniKani uses is matched. The span keeps the text's own characters,
        and `word` is the dictionary form the card shows. Never raises.
        """
        if not text or not self._words:
            return []
        try:
            hits = self._token_spans(text) if self.tokens is not None else self._exact_spans(text)
        except Exception:  # noqa: BLE001 - a colour is never worth a lost sentence
            return []
        return sorted(({"start": s, "end": e, "word": item.text, "reading": item.reading,
                        "meaning": item.meaning, "stage": stage_name(item.stage)}
                       for s, e, item in hits), key=lambda x: x["start"])

    def _token_spans(self, text: str) -> list[tuple[int, int, Item]]:
        toks = [t for t in (self.tokens(text) if self.tokens else []) if t[1] > t[0]]
        out: list[tuple[int, int, Item]] = []
        i = 0
        while i < len(toks):
            hit = None
            for n in range(min(_MAX_JOIN, len(toks) - i), 0, -1):
                run = toks[i:i + n]
                if any(run[k][1] != run[k + 1][0] for k in range(n - 1)):     # not adjacent
                    continue
                surface = text[run[0][0]:run[-1][1]]
                candidates = [surface, "".join(t[2] for t in run)]
                if n == 1:
                    candidates += [run[0][2], run[0][3]]
                for c in candidates:
                    if item := self._words.get(c):
                        hit = (run[0][0], run[-1][1], item)
                        break
                if hit:
                    i += n
                    break
            if hit:
                out.append(hit)
            else:
                i += 1
        return out

    def _exact_spans(self, text: str) -> list[tuple[int, int, Item]]:
        taken = [False] * len(text)
        out: list[tuple[int, int, Item]] = []
        for word in self._exact:
            for m in re.finditer(re.escape(word), text):
                if any(taken[m.start():m.end()]):
                    continue
                taken[m.start():m.end()] = [True] * (m.end() - m.start())
                out.append((m.start(), m.end(), self._words[word]))
        return out

    def kind_of(self, phrase: str) -> str:
        """"vocab", "grammar", or "" — what the tutor's `[used:…]` names, if it is really theirs.

        The float behind her is for something they are still learning (user, 2026-09-12); a word
        they Guru'd two months ago is not a win worth celebrating, and neither is one the tutor
        invented. Falls back to a containment match, because she writes 「〜たら」 for 「たら」.
        """
        key = normalise(phrase)
        if not key:
            return ""
        if item := self._by_key.get(key):
            return item.kind
        for other, item in self._by_key.items():
            if other and (other in key or key in other):
                return item.kind
        return ""
