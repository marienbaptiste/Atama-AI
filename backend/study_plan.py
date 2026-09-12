"""Today's targets: which of their unmastered items this lesson works on, and what comes next
(spec §6c, ADR-038 — user request 2026-09-12: "lessons revolve around a few news items; I want
more diversity and coverage of ALL the vocabulary and grammar I have not mastered, rotating").

Everything here is deterministic and costs zero model calls. The inputs already exist: the SRS
snapshot fetched at launch (never a new call, ADR-024) says what they are still learning; the turn
logs memory.py already writes say what has been practised — the tutor's `{{span|point}}` grammar
marks, her `[target:]` and `[used:]` marks, and the student's own transcript matched against their
words by `backend.study`. Folding those logs is milliseconds, so nothing is persisted: the fold is
recomputed at every launch and is the source of truth.

    fold(records, items)     the coverage ledger, replayed session by session
    select(...)              this lesson's targets and the ranked queue behind them
    Plan.note_turn(record)   after each turn: counts, retires a produced target, promotes the next
    opener_for(index)        news | scenario | personal | story, one per lesson in turn
    render(plan)             the TODAY'S TARGETS block for the prompt (≤ PLAN_MAX_TOKENS)
    coach_note(plan, turn)   the short bracketed note prepended to the student's text every few turns

**It behaves like an SRS** (user refinement, 2026-09-12). A target the student produced correctly
`STUDY_PROGRESS_AFTER` times in a session is a success round: it goes away and comes back after
`STUDY_SPACING_BASE × 2^(streak-1)` sessions, capped at `STUDY_SPACING_MAX` — 1, 2, 4, 8, 16, 32.
One they attempted and never got right is a lapse: the streak resets and it is back next session.
One the tutor used but they never tried is unchanged, still due. So the well-handled items get
rarer and the painful ones keep returning, and every item is visited: a target that had a success
round within the last `RECENT_DAYS` goes to the back of the due list whatever its weakness (or the
weakest few would come straight back at a one-session gap and the rest would never get a turn),
then the weakest come first, never-covered before covered, then the most overdue.

The words the tutor reads — the block, the opener lines, the coach note — are in
`prompts/coach.md` (ADR-012). The scenarios are `backend/data/scenarios.txt`.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from backend import config
from backend import memory as memory_api
from backend.srs.profile import estimate_tokens
from backend.study import MIN_CHARS, Item, Study, normalise

#: How many past sessions the ledger replays. Older practice is forgotten, which is what an SRS
#: does with a lapse anyway; enough sessions that a 32-session gap can still be observed.
SESSIONS_FOLDED = 60
#: The four ways a lesson can open, one per lesson in this order (spec §6c). Only `news` may
#: search; the others cost nothing and keep the lessons from being four news items in a row.
OPENER_KINDS = ("news", "scenario", "personal", "story")
SCENARIOS_FILE = config.REPO_ROOT / "backend" / "data" / "scenarios.txt"
WORDING_FILE = config.REPO_ROOT / "prompts" / "coach.md"
#: The TODAY'S TARGETS block's budget in the prompt (spec §6c). It is read on every turn.
PLAN_MAX_TOKENS = 250
#: The coach note's budget. It rides in the user turn, so it is paid every time it is sent.
NOTE_MAX_TOKENS = 40
#: How many not-yet-used targets a note names before it says nothing more.
PENDING_SHOWN = 3
#: A meaning is cut here in the block: "savings" is enough, "savings, to save money, deposit" is not.
MEANING_CHARS = 18
#: Scenarios are picked by striding through the list by the scenario ordinal (every fourth lesson
#: is one): the first stride from 7 coprime with the list's length, so every line is reached before
#: any repeats, whatever the length.
SCENARIO_STRIDE = 7
#: A target that had a success round this recently goes to the back of the due list, so the whole
#: list gets visited rather than the weakest few every lesson (spec §6c). Lapses are never pushed
#: back: a painful item comes back soon.
RECENT_DAYS = 7
#: Weakness order within grammar (spec §6c): a ghost first, then Bunpro's stages upward. Unknown
#: stages sort last.
_GRAMMAR_STAGES = {"ghost": 0, "beginner": 2, "adept": 3, "seasoned": 4}
#: Sessions between rounds are 1, 2, 4, 8… — the exponent is the streak.
_DEFAULTS = {"progress_after": 2, "spacing_base": 1, "spacing_max": 32,
             "target_vocab": 8, "target_grammar": 4, "nudge_every": 3}


# ------------------------------------------------------------------------ the ledger
@dataclass
class Entry:
    """One item's history across every folded session."""

    key: str
    heard: int = 0            # turns in which the tutor used or asked for it
    produced: int = 0         # turns in which the student used it correctly ([used:] mark)
    attempted: int = 0        # turns in which it appeared in the student's transcript
    last_seen: str = ""       # ISO dates; "" = never
    last_produced: str = ""
    #: The SRS part: consecutive success rounds, lapses, and the session index it is due again at.
    streak: int = 0
    lapses: int = 0
    due_session: int = 0
    progressed: str = ""      # the date of the last success round

    @property
    def covered(self) -> bool:
        return bool(self.heard or self.produced)


@dataclass
class Ledger:
    entries: dict[str, Entry] = field(default_factory=dict)
    #: The index of the session about to happen: how many sessions the fold saw.
    today: int = 0
    #: The date of that session (ISO), for "progressed recently". Given, so nothing here reads the clock twice.
    date: str = field(default_factory=lambda: dt.date.today().isoformat())

    def entry(self, key: str) -> Entry:
        return self.entries.setdefault(key, Entry(key))

    def get(self, key: str) -> Entry:
        return self.entries.get(key) or Entry(key)


def days_between(earlier: str, later: str) -> int | None:
    """Days from one ISO date to another; None when either is not a date."""
    try:
        return (dt.date.fromisoformat(later[:10]) - dt.date.fromisoformat(earlier[:10])).days
    except ValueError:
        return None


def recently_progressed(entry: Entry, date: str) -> bool:
    """A success round within RECENT_DAYS of `date` — the ledger's, or the plan's."""
    if not entry.progressed:
        return False
    days = days_between(entry.progressed, date)
    return days is not None and 0 <= days < RECENT_DAYS


def gap_after(streak: int, base: int, cap: int) -> int:
    """Sessions until an item is due again after its `streak`-th success round: base × 2^(streak-1),
    never below 1, never above the cap."""
    return max(1, min(int(cap), int(base) * 2 ** max(0, int(streak) - 1)))


def key_of(item: Item) -> str:
    return normalise(item.text)


def weakness(item: Item) -> int:
    """Lower is weaker: ghost < leech < the lower SRS stage (spec §6c ranking)."""
    if item.kind == "grammar":
        return _GRAMMAR_STAGES.get(str(item.srs or "").lower(), 5)
    if item.leech:
        return 1
    return 2 + max(0, int(item.stage or 0))


def resolve(phrase: str, keys: Iterable[str]) -> str:
    """The item key a mark names, or "". Exact first; otherwise the longest key that contains or is
    contained by it, because she writes 「〜たら」 for 「たら」 and 「食べてみる」 for 「〜てみる」."""
    want = normalise(phrase)
    if not want:
        return ""
    keys = list(keys)
    if want in keys:
        return want
    found = [k for k in keys if len(k) >= MIN_CHARS and (k in want or want in k)]
    return max(found, key=len) if found else ""


@dataclass
class TurnFacts:
    """What one turn record says about the items, keyed like the ledger."""

    heard: set[str] = field(default_factory=set)
    produced: set[str] = field(default_factory=set)
    attempted: set[str] = field(default_factory=set)
    date: str = ""


def scan_turn(record: dict[str, Any], study: Study) -> TurnFacts:
    """Read one turn record (the §6b schema) for the items it touched. Never raises."""
    facts = TurnFacts(date=str(record.get("ts") or "")[:10])
    keys = list(study._by_key)
    grammar_keys = [k for k, i in study._by_key.items() if i.kind == "grammar" and len(k) >= MIN_CHARS]
    tutor = record.get("tutor") or {}
    for sentence in tutor.get("sentences") or []:
        if not isinstance(sentence, dict):
            continue
        text = str(sentence.get("text") or "")
        for span in study.spans(text):
            facts.heard.add(normalise(span["word"]))
        for mark in sentence.get("grammar") or []:
            if isinstance(mark, dict) and (k := resolve(str(mark.get("point") or ""), keys)):
                facts.heard.add(k)
        if k := resolve(str(sentence.get("target") or ""), keys):
            facts.heard.add(k)
        if k := resolve(str(sentence.get("used") or ""), keys):
            facts.produced.add(k)
    student = str((record.get("student") or {}).get("text") or "")
    if student:
        for span in study.spans(student):
            facts.attempted.add(normalise(span["word"]))
        flat = normalise(student)
        for k in grammar_keys:
            if k in flat:
                facts.attempted.add(k)
    facts.attempted |= facts.produced        # credited is attempted, whatever the matcher saw
    return facts


def fold(records: Iterable[dict[str, Any]], items: Iterable[Item], *, progress_after: int = 2,
         spacing_base: int = 1, spacing_max: int = 32, sessions: int = SESSIONS_FOLDED,
         date: str = "") -> Ledger:
    """The coverage ledger from turn records of any number of sessions, any tutor.

    Sessions are replayed in the order they happened (the earliest `ts` of each), the last
    `sessions` of them, and each closes with the SRS rule: a target produced `progress_after`
    times is a success round (streak up, due doubled), one attempted and never produced is a lapse
    (streak reset, due next session), one only heard is unchanged.
    """
    study = Study(items)
    by_session: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        if isinstance(row, dict):
            by_session.setdefault(str(row.get("session") or ""), []).append(row)
    order = sorted(by_session.values(), key=lambda rows: min(str(r.get("ts") or "") for r in rows))
    order = order[-max(0, int(sessions)):] if sessions else order
    ledger = Ledger(today=len(order), **({"date": date} if date else {}))
    for index, rows in enumerate(order):
        heard_n: Counter[str] = Counter()
        produced_n: Counter[str] = Counter()
        attempted_n: Counter[str] = Counter()
        date = ""
        for row in sorted(rows, key=lambda r: (str(r.get("ts") or ""), int(r.get("turn") or 0))):
            f = scan_turn(row, study)
            date = f.date or date
            for k in f.heard:
                e = ledger.entry(k)
                e.heard += 1
                e.last_seen = max(e.last_seen, f.date)
                heard_n[k] += 1
            for k in f.produced:
                e = ledger.entry(k)
                e.produced += 1
                e.last_produced = max(e.last_produced, f.date)
                e.last_seen = max(e.last_seen, f.date)
                produced_n[k] += 1
            for k in f.attempted:
                ledger.entry(k).attempted += 1
                attempted_n[k] += 1
        for k in sorted(set(heard_n) | set(produced_n)):
            e = ledger.entry(k)
            if produced_n[k] >= progress_after:
                e.streak += 1
                e.due_session = index + gap_after(e.streak, spacing_base, spacing_max)
                e.progressed = date
            elif produced_n[k] == 0 and attempted_n[k] > 0:
                e.streak = 0
                e.lapses += 1
                e.due_session = index + 1
    return ledger


def load_ledger(sessions: Path, items: Iterable[Item], *, exclude_session: str = "", **kw: Any) -> Ledger:
    """The ledger from every session log on disk (all tutors: the student is the same). Never
    raises — an unreadable log is a session forgotten, not a launch lost."""
    records: list[dict[str, Any]] = []
    try:
        for log in sorted(Path(sessions).glob("*.jsonl")):
            if exclude_session and log.stem.endswith(exclude_session):
                continue
            records.extend(memory_api.read_jsonl(log))
    except OSError:
        pass
    return fold(records, items, **kw)


# ---------------------------------------------------------------------- selection
@dataclass
class Target:
    item: Item
    key: str
    heard: int = 0          # this session
    produced: int = 0       # this session


@dataclass
class Change:
    """What a turn changed: targets retired (with how many sessions until they return) and the
    candidates promoted in their place."""

    retired: list[tuple[Item, int]] = field(default_factory=list)
    promoted: list[Item] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.retired or self.promoted)


@dataclass(frozen=True)
class Opener:
    kind: str
    subject: str = ""


@dataclass
class Plan:
    vocab: list[Target]
    grammar: list[Target]
    queue: dict[str, list[Item]]
    opener: Opener
    ledger: Ledger
    study: Study
    date: str
    progress_after: int = 2
    spacing_base: int = 1
    spacing_max: int = 32
    nudge_every: int = 3
    #: How many of each kind the lesson wants: a promotion keeps the count, a retirement with an
    #: empty queue lowers it, and a re-selection (Refresh) asks for this again.
    want: dict[str, int] = field(default_factory=dict)
    turns: int = 0
    retired: list[tuple[Item, int]] = field(default_factory=list)
    #: The `turns` count at the last progression, so the next coach note can say it.
    changed_at: int = -1
    last_change: Change = field(default_factory=Change)

    @property
    def targets(self) -> list[Target]:
        return [*self.vocab, *self.grammar]

    def note_turn(self, record: dict[str, Any]) -> Change:
        """After a turn, with the record memory got: count, retire what was produced enough,
        promote the next candidate of the same kind. Returns what changed."""
        self.turns += 1
        f = scan_turn(record, self.study)
        for k in f.heard | f.produced | f.attempted:
            e = self.ledger.entry(k)
            if k in f.heard:
                e.heard += 1
                e.last_seen = max(e.last_seen, self.date)
            if k in f.produced:
                e.produced += 1
                e.last_produced = max(e.last_produced, self.date)
                e.last_seen = max(e.last_seen, self.date)
            if k in f.attempted:
                e.attempted += 1
        change = Change()
        for kind, targets in (("vocab", self.vocab), ("grammar", self.grammar)):
            for t in targets:
                if t.key in f.heard:
                    t.heard += 1
                if t.key in f.produced:
                    t.produced += 1
            for t in list(targets):
                if t.produced < self.progress_after:
                    continue
                e = self.ledger.entry(t.key)
                e.streak += 1
                gap = gap_after(e.streak, self.spacing_base, self.spacing_max)
                e.due_session = self.ledger.today + gap      # cannot be re-selected this session
                e.progressed = self.date
                targets.remove(t)
                self.retired.append((t.item, gap))
                change.retired.append((t.item, gap))
                if (nxt := self._promote(kind)) is not None:
                    targets.append(Target(nxt, key_of(nxt)))
                    change.promoted.append(nxt)
        if change:
            self.changed_at = self.turns
            self.last_change = change
        return change

    def _promote(self, kind: str) -> Item | None:
        """The next candidate of that kind: the queue is ranked due-first, soonest-due after."""
        queue = self.queue.get(kind) or []
        taken = {t.key for t in self.targets}
        for i, item in enumerate(queue):
            if key_of(item) not in taken:
                return queue.pop(i)
        return None

    def reselect(self, items: Iterable[Item]) -> "Plan":
        """The same session over a new item set (Refresh, a tutor switch): re-ranked from the same
        ledger, keeping this session's counts, its retirements and its opener."""
        new = select(items, self.ledger, target_vocab=self.want.get("vocab", len(self.vocab)),
                     target_grammar=self.want.get("grammar", len(self.grammar)),
                     progress_after=self.progress_after, spacing_base=self.spacing_base,
                     spacing_max=self.spacing_max, nudge_every=self.nudge_every, opener=self.opener,
                     date=self.date)
        counts = {t.key: (t.heard, t.produced) for t in self.targets}
        for t in new.targets:
            t.heard, t.produced = counts.get(t.key, (0, 0))
        new.turns, new.retired = self.turns, list(self.retired)
        new.changed_at, new.last_change = self.changed_at, self.last_change
        return new


def rank_key(item: Item, ledger: Ledger) -> tuple:
    """Recently progressed last; then weakness, never-covered, most overdue, most lapses, fewest
    productions, then the text — so two launches over the same logs pick the same targets."""
    e = ledger.get(key_of(item))
    return (1 if recently_progressed(e, ledger.date) else 0, weakness(item), 1 if e.covered else 0,
            e.due_session, -e.lapses, e.produced, item.text)


def ranked(items: Iterable[Item], ledger: Ledger, kind: str) -> list[Item]:
    """Candidates of one kind: the due ones by rank, then the rest soonest-due first."""
    seen: set[str] = set()
    pool: list[Item] = []
    for item in items:
        k = key_of(item)
        if item.kind == kind and k and k not in seen:
            seen.add(k)
            pool.append(item)
    due = [i for i in pool if ledger.get(key_of(i)).due_session <= ledger.today]
    later = [i for i in pool if ledger.get(key_of(i)).due_session > ledger.today]
    due.sort(key=lambda i: rank_key(i, ledger))
    later.sort(key=lambda i: (ledger.get(key_of(i)).due_session, rank_key(i, ledger)))
    return due + later


def select(items: Iterable[Item], ledger: Ledger, *, target_vocab: int = 8, target_grammar: int = 4,
           progress_after: int = 2, spacing_base: int = 1, spacing_max: int = 32, nudge_every: int = 3,
           opener: Opener | None = None, date: str = "") -> Plan:
    """This lesson's targets: the top of each kind's ranking, with the rest as the queue."""
    items = list(items)
    picked: dict[str, list[Target]] = {}
    queue: dict[str, list[Item]] = {}
    for kind, n in (("vocab", max(0, int(target_vocab))), ("grammar", max(0, int(target_grammar)))):
        order = ranked(items, ledger, kind)
        picked[kind] = [Target(i, key_of(i)) for i in order[:n]]
        queue[kind] = order[n:]
    return Plan(vocab=picked["vocab"], grammar=picked["grammar"], queue=queue,
                want={"vocab": max(0, int(target_vocab)), "grammar": max(0, int(target_grammar))},
                opener=opener or Opener("news"), ledger=ledger, study=Study(items),
                date=date or ledger.date, progress_after=int(progress_after),
                spacing_base=int(spacing_base), spacing_max=int(spacing_max), nudge_every=int(nudge_every))


def build(items: Iterable[Item], ledger: Ledger, cfg: Any, *, session_index: int, recent_topics: Iterable[str] = (),
          facts: Iterable[str] = (), date: str = "", scenarios: list[str] | None = None) -> Plan:
    """Selection plus the opener, from the config's STUDY_* keys."""
    items = list(items)
    plan = select(items, ledger, target_vocab=_cfg(cfg, "STUDY_TARGET_VOCAB", "target_vocab"),
                  target_grammar=_cfg(cfg, "STUDY_TARGET_GRAMMAR", "target_grammar"),
                  progress_after=_cfg(cfg, "STUDY_PROGRESS_AFTER", "progress_after"),
                  spacing_base=_cfg(cfg, "STUDY_SPACING_BASE", "spacing_base"),
                  spacing_max=_cfg(cfg, "STUDY_SPACING_MAX", "spacing_max"),
                  nudge_every=_cfg(cfg, "STUDY_NUDGE_EVERY", "nudge_every"), date=date)
    plan.opener = opener_for(session_index, scenarios=scenarios, recent_topics=recent_topics, facts=facts,
                             targets=[t.item for t in plan.targets])
    return plan


def _cfg(cfg: Any, key: str, default: str) -> int:
    try:
        return int(getattr(cfg, key))
    except (AttributeError, TypeError, ValueError):
        return _DEFAULTS[default]


# ------------------------------------------------------------------------ the opener
def load_scenarios(path: Path = SCENARIOS_FILE) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def pick_scenario(session_index: int, scenarios: list[str], recent_topics: Iterable[str] = ()) -> str:
    """Stride through the list by the scenario ordinal — the session index over the number of
    opener kinds, since one lesson in four is a scenario; skip a line that overlaps a recent topic."""
    if not scenarios:
        return ""
    n = len(scenarios)
    recent = [normalise(t) for t in recent_topics if len(normalise(t)) >= MIN_CHARS]
    stride = next(s for s in range(SCENARIO_STRIDE, SCENARIO_STRIDE + n + 1) if math.gcd(s, n) == 1)
    start = ((int(session_index) // len(OPENER_KINDS)) * stride) % n
    for i in range(n):
        line = scenarios[(start + i) % n]
        flat = normalise(line)
        if not any(r in flat or flat in r for r in recent):
            return line
    return scenarios[start]


def opener_for(session_index: int, *, scenarios: list[str] | None = None, recent_topics: Iterable[str] = (),
               facts: Iterable[str] = (), targets: Iterable[Item] = ()) -> Opener:
    """news, scenario, personal, story — in turn, one per lesson. `personal` needs a remembered
    fact and falls back to `scenario`; `scenario` with no list falls back to `news`."""
    facts = [f for f in facts if str(f).strip()]
    scenarios = load_scenarios() if scenarios is None else scenarios
    kind = OPENER_KINDS[int(session_index) % len(OPENER_KINDS)]
    if kind == "personal" and not facts:
        kind = "scenario"
    if kind == "scenario" and not scenarios:
        kind = "news"
    if kind == "scenario":
        return Opener(kind, pick_scenario(session_index, scenarios, recent_topics))
    if kind == "personal":
        return Opener(kind, str(facts[int(session_index) % len(facts)]))
    if kind == "story":
        targets = list(targets)
        chosen = [i for i in targets if i.kind == "vocab"][:2] + [i for i in targets if i.kind == "grammar"][:1]
        chosen = chosen or targets[:3]
        return Opener(kind, "、".join(i.text for i in chosen))
    return Opener("news")


# ---------------------------------------------------------------------- the prompt
def wording(path: Path = WORDING_FILE) -> dict[str, str]:
    """The `## key` blocks of prompts/coach.md; a missing key renders as its name in capitals."""
    return memory_api.memory_headings(path)


def _meaning(item: Item) -> str:
    first = str(item.meaning or "").split(",")[0].strip()
    return first[:MEANING_CHARS]


def _vocab_line(item: Item, detail: int) -> str:
    if detail >= 2 and item.reading and _meaning(item):
        return f"{item.text}（{item.reading}）{_meaning(item)}"
    if detail >= 1 and item.reading:
        return f"{item.text}（{item.reading}）"
    return item.text


def render(plan: Plan) -> str:
    """The TODAY'S TARGETS block, within PLAN_MAX_TOKENS: meanings go first, then readings, then
    the tail of the vocab list, so the block is never truncated mid-line by prompt.build."""
    say = wording()
    vocab, grammar = [t.item for t in plan.vocab], [t.item for t in plan.grammar]
    opener = say["opener"] + " " + say["opener_" + plan.opener.kind] + (" " + plan.opener.subject if plan.opener.subject else "")

    def text(detail: int, keep: int) -> str:
        lines = []
        if vocab or grammar:
            lines.append(say["heading"])
        if vocab[:keep]:
            lines.append(say["vocab"] + " " + "、".join(_vocab_line(i, detail) for i in vocab[:keep]))
        if grammar:
            lines.append(say["grammar"] + " " + "、".join(i.text for i in grammar))
        lines.append(opener)
        if vocab or grammar:
            lines.append(say["rule"])
        return "\n".join(lines)

    for detail in (2, 1, 0):
        if estimate_tokens(out := text(detail, len(vocab))) <= PLAN_MAX_TOKENS:
            return out
    keep = len(vocab)
    while keep > 0 and estimate_tokens(out := text(0, keep)) > PLAN_MAX_TOKENS:
        keep -= 1
    return out


def coach_note(plan: Plan, turn_index: int) -> str:
    """The bracketed note for the turn about to be sent, or "": every `nudge_every` turns, and
    always on the turn after a progression. English keywords, so it is never taken for speech."""
    every = int(plan.nudge_every or 0)
    changed = plan.changed_at == turn_index and bool(plan.last_change)
    if not changed and (every <= 0 or turn_index <= 0 or turn_index % every != 0):
        return ""
    say = wording()
    fixed: list[str] = []
    if changed:
        promoted = list(plan.last_change.promoted)
        for old, gap in plan.last_change.retired:
            new = next((p for p in promoted if p.kind == old.kind), None)
            if new is not None:
                promoted.remove(new)
                fixed.append(say["progressed"].replace("{{old}}", old.text).replace("{{n}}", str(gap))
                             .replace("{{new}}", new.text))
            else:
                fixed.append(say["done"].replace("{{old}}", old.text).replace("{{n}}", str(gap)))
    pending = [t.item.text for t in plan.targets if t.heard == 0]
    elicit = next((t.item.text for t in plan.targets if t.heard and not t.produced), "")
    for shown in range(PENDING_SHOWN, -1, -1):
        parts = list(fixed)
        if pending[:shown]:
            parts.append(say["pending"].replace("{{items}}", "、".join(pending[:shown])))
        if elicit:
            parts.append(say["elicit"].replace("{{item}}", elicit))
        if not parts:
            return ""
        note = say["note"].replace("{{body}}", "; ".join(parts))
        if estimate_tokens(note) <= NOTE_MAX_TOKENS:
            return note
    parts = fixed or ([say["elicit"].replace("{{item}}", elicit)] if elicit else [])
    return say["note"].replace("{{body}}", "; ".join(parts)) if parts else ""


def coached(plan: Plan | None, text: str) -> str:
    """The text the brain is asked: the coach note above the student's words, when one is due.
    The transcript itself is untouched — the log, the page and the TTS never see the note."""
    if plan is None:
        return text
    note = coach_note(plan, plan.turns)
    return f"{note}\n{text}" if note else text


# -------------------------------------------------------------------- the summariser
def summary_footer(log: Path, progress_after: int = 2) -> str:
    """Two lines under a lesson's transcript for the summariser (spec §6c): what the tutor's own
    marks say was practised, and what was produced often enough to progress. From the log alone —
    no item set, so it needs nothing the next launch may not have."""
    practised: list[str] = []
    produced: Counter[str] = Counter()
    for row in memory_api.read_jsonl(log):
        for s in (row.get("tutor") or {}).get("sentences") or []:
            if not isinstance(s, dict):
                continue
            for mark in s.get("grammar") or []:
                if isinstance(mark, dict) and (p := str(mark.get("point") or "").strip()):
                    practised.append(p)
            if t := str(s.get("target") or "").strip():
                practised.append(t)
            if u := str(s.get("used") or "").strip():
                produced[u] += 1
    say = wording()
    lines = []
    if names := list(dict.fromkeys(practised)):
        lines.append(say["practised"] + " " + "、".join(names[:12]))
    if done := [k for k, n in produced.items() if n >= int(progress_after)]:
        lines.append(say["progressed_line"] + " " + "、".join(done[:12]))
    return "\n".join(lines)
