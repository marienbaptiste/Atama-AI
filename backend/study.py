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
from typing import Any, Iterable

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


#: WaniKani's own names for the stages (STAGE_BUCKETS in srs/wanikani.py), for the word card.
_STAGES = {1: "Apprentice 1", 2: "Apprentice 2", 3: "Apprentice 3", 4: "Apprentice 4",
           5: "Guru 1", 6: "Guru 2", 7: "Master", 8: "Enlightened", 9: "Burned"}


def stage_name(stage: int) -> str:
    """"Apprentice 2" for 2, "" for anything that is not a WaniKani stage."""
    return _STAGES.get(int(stage or 0), "")


def normalise(phrase: str) -> str:
    """A grammar point or word reduced to what two spellings of it have in common."""
    return _TRIM.sub("", str(phrase or "")).strip()


#: Verb and adjective endings that inflect: 気に入る -> 気に入ります, 難しい -> 難しかった. Dropping the
#: last character leaves a prefix every inflection of that word starts with, which is all the match
#: needs — it is looking for "one of theirs, in play", not parsing morphology.
_INFLECTS = "るういくぐすつぬぶむ"


def written_forms(item: "Item") -> list[str]:
    """How this word can appear in a sentence: as WaniKani writes it, as its kana reading (she
    writes 竹 as たけ when the kanji is above their level), and as the prefix every inflection
    shares — 勉強する also matches 勉強します, 難しい also matches 難しかった."""
    forms = [item.text]
    reading = (item.reading or "").replace("、", " ").split()
    forms += [r for r in reading[:1] if r and r != item.text]
    for base in list(forms):
        stem = base[:-2] if base.endswith("する") else (base[:-1] if base[-1:] in _INFLECTS else "")
        if len(stem) >= MIN_CHARS:
            forms.append(stem)
    return [f for f in dict.fromkeys(forms) if len(f) >= MIN_CHARS]


class Study:
    """The student's own words and grammar points, and where they appear in a sentence."""

    def __init__(self, items: Iterable[Item] = ()) -> None:
        self.items: list[Item] = list(items)[:MAX_ITEMS]
        #: Every written form of every word, longest first, so 勉強する is marked before 勉強 and
        #: an inflected 気に入ります is caught by its stem.
        forms: dict[str, Item] = {}
        for item in self.items:
            if item.kind != "vocab":
                continue
            for form in written_forms(item):
                forms.setdefault(form, item)
        self._forms = sorted(forms.items(), key=lambda kv: len(kv[0]), reverse=True)
        self._by_key = {normalise(i.text): i for i in self.items}

    @classmethod
    def from_profile(cls, profile: Any) -> "Study":
        """Built from the snapshot already in memory. Never fetches (ADR-024) and never raises."""
        items: list[Item] = []
        try:
            wk = getattr(profile, "wanikani", None)
            seen: set[str] = set()
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
                                  int(getattr(vocab, "srs_stage", 0) or 0)))
            bp = getattr(profile, "bunpro", None)
            # in_play is everything still in their SRS (ghost, beginner, adept, seasoned); the
            # other two are its head, and the fallback on an older snapshot.
            for point in (list(getattr(bp, "in_play", []) or [])
                          + list(getattr(bp, "ghosts", []) or [])
                          + list(getattr(bp, "weak_grammar", []) or [])):
                title = str(getattr(point, "title", "") or "")
                if title and normalise(title) not in {normalise(i.text) for i in items}:
                    items.append(Item(title, "grammar", meaning=str(getattr(point, "meaning", "") or "")))
        except Exception:  # noqa: BLE001 - a colour is never worth a lost sentence
            return cls(items)
        return cls(items)

    def spans(self, text: str) -> list[dict[str, Any]]:
        """Every word of theirs in `text`: [{start, end, word}] over code points, no overlaps.

        A word is matched as WaniKani writes it, as its kana reading (she writes 竹 as たけ when the
        kanji is above their level — the prompt tells her to), and by its stem, so 「気に入ります」
        and 「勉強しました」 count as 気に入る and 勉強する. Longest form first; the span keeps the
        text's own characters, and `word` is the dictionary form the card would show.
        """
        if not text or not self._forms:
            return []
        taken = [False] * len(text)
        out: list[dict[str, Any]] = []
        for form, item in self._forms:
            for m in re.finditer(re.escape(form), text):
                if any(taken[m.start():m.end()]):
                    continue
                taken[m.start():m.end()] = [True] * (m.end() - m.start())
                out.append({"start": m.start(), "end": m.end(), "word": item.text,
                            "reading": item.reading, "meaning": item.meaning,
                            "stage": stage_name(item.stage)})
        return sorted(out, key=lambda s: s["start"])

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
