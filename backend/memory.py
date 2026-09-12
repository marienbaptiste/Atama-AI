"""Cross-session memory (spec §6b, ADR-031).

Four tiers, each with exactly one read moment and one write moment, none on the critical path:

    tier                where                                 read               written
    turn log            logs/sessions/<date>-<session>.jsonl  never by the tutor in the speaking gap
    student notes       <state>/memory/student.md             session start      after summarising
    last-session brief  <state>/memory/last-session.md        session start      after summarising
    recent topics       <state>/memory/topics.jsonl           session start      after summarising
    what you both know  <state>/memory/facts.md               session start      after summarising

The fourth tier is the one that keeps lessons from repeating themselves: a few short noun phrases
per session, read at start as "recently discussed — open on something else" (2026-09-10).

The fifth is what makes two people who have met before sound like it (user, 2026-09-12): the
student's name, where they live, their work and their cat — and, on the other side, everything the
tutor has said about their own life, so they do not acquire a second pet next week (the tutor is a
man or a woman depending on the chosen voice, so nothing here assumes). Deliberately small:
a handful of durable lines, not a diary.

Summarising happens at the NEXT launch, during start-up, for any session log that has not been
summarised yet — not on exit. Exiting has to be instant (Ctrl+C that hangs for fifteen seconds
reads as a crash), and ADR-031 already required the next launch to rebuild from the log if the
app died first; this makes that the normal path rather than the recovery path.

Everything here is best-effort. A memory operation that fails costs the tutor some recall; it must
never cost the student a turn. So nothing in this module raises past its own boundary.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from backend import config

#: The words the tutor reads over each memory tier — a file, like every other instruction the
#: model reads (ADR-012). See render() and memory_headings().
HEADINGS_FILE = config.REPO_ROOT / "prompts" / "memory.md"

#: How many past sessions' topics to show. Enough to stop a week of lessons opening on the same
#: typhoon; few enough to stay a single short line in the prompt.
RECENT_SESSIONS = 8
#: Topics kept per session by the summariser. Short noun phrases, not sentences.
TOPICS_PER_SESSION = 5
#: Durable facts kept per side. Small on purpose (user, 2026-09-12: "not a really long one"): this
#: is who you are to each other, not a transcript. Over the cap the OLDEST survive — a name and a
#: country are learned in the first lesson and must not be pushed out by last Tuesday's cake — with
#: the last few slots left for what is new.
STUDENT_FACTS = 10
TUTOR_FACTS = 6
#: Newest facts guaranteed a slot even when the list is full.
FACTS_FRESH = 3
#: One line each. A fact that needs a paragraph is a note, not a fact.
FACT_MAX_CHARS = 90

#: The summariser reads a text-only excerpt of the log, capped so a long lesson cannot make the
#: once-per-session summary expensive.
EXCERPT_MAX_CHARS = 6000

_LOCK = threading.Lock()


def memory_dir(cfg) -> Path:
    """<state>/memory — beside the claude cwd, which config already guarantees is outside the
    repository (a memory file inside it could be committed, and it holds the student's life)."""
    return config.claude_cwd(cfg).parent / "memory"


def persona_slug(persona: str) -> str:
    """One directory per tutor. A persona may be given as a path (spec §6), so take its stem and
    keep it to safe characters; anything unnamed falls back to a single shared drawer."""
    stem = Path(str(persona or "")).stem.strip().lower()
    return re.sub(r"[^a-z0-9_-]+", "-", stem).strip("-") or "tutor"


def sessions_dir(cfg) -> Path:
    return cfg.path("LOG_DIR") / "sessions"


@dataclass
class Memory:
    root: Path              # <state>/memory/<tutor> — this tutor's own memory
    sessions: Path          # logs/sessions
    session_id: str = ""
    turn: int = 0
    persona: str = ""
    #: <state>/memory — what is true of the student, whoever is teaching. Defaults to `root`, so a
    #: Memory built by hand (tests, tools) keeps everything in one directory as before.
    shared: Path | None = None
    #: The calendar day the lesson began. Fixed at construction so one lesson is ONE log file:
    #: taking today's date at each write split a lesson crossing midnight into two files with
    #: the same session id, and once the first was summarised the second never was (2026-09-12).
    date: str = field(default_factory=lambda: dt.date.today().isoformat())

    @classmethod
    def from_config(cls, cfg, session_id: str = "", persona: str | None = None) -> "Memory":
        who = persona_slug(getattr(cfg, "TUTOR_PERSONA", "") if persona is None else persona)
        shared = memory_dir(cfg)
        mem = cls(root=shared / who, sessions=sessions_dir(cfg), session_id=session_id,
                  persona=who, shared=shared)
        mem.migrate()
        return mem

    def for_persona(self, persona: str) -> "Memory":
        """The same student, a different tutor — a second Memory over the same shared root."""
        who = persona_slug(persona)
        mem = Memory(root=self._shared / who, sessions=self.sessions, session_id=self.session_id,
                     turn=self.turn, persona=who, shared=self._shared)
        mem.migrate()
        return mem

    def switch_to(self, persona: str) -> None:
        """Become that tutor's memory, in place: the student changed teacher mid-session and
        everything already holding this object — the turn recorder, the handoff — must follow."""
        who = persona_slug(persona)
        if who == self.persona:
            return
        self.persona, self.root = who, self._shared / who
        self.migrate()

    def migrate(self) -> None:
        """Move a pre-persona memory (everything loose in <state>/memory) into this tutor's
        drawer. It was written by whoever was teaching then, and that is who is teaching now."""
        if self.shared is None or self.root == self._shared or self.root.exists():
            return
        try:
            loose = [q for q in (self._shared / "last-session.md", self._shared / "topics.jsonl",
                                 self._shared / "facts.md") if q.exists()]
            if not loose:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            for path in loose:
                path.replace(self.root / path.name)
        except OSError:
            pass                # a memory that fails to move is recall lost, never a lost lesson

    # ------------------------------------------------------------------ paths
    @property
    def _shared(self) -> Path:
        return self.shared if self.shared is not None else self.root

    @property
    def student_md(self) -> Path:
        return self._shared / "student.md"

    @property
    def about_md(self) -> Path:
        """The student's own durable facts: the same person, whoever is teaching them."""
        return self._shared / "about-me.md"

    @property
    def brief_md(self) -> Path:
        return self.root / "last-session.md"

    @property
    def facts_md(self) -> Path:
        return self.root / "facts.md"

    @property
    def topics_jsonl(self) -> Path:
        return self.root / "topics.jsonl"

    def log_path(self) -> Path:
        return self.sessions / f"{self.date}-{self.session_id}.jsonl"

    # --------------------------------------------------------------- turn log
    def record_turn(self, *, student: str, tutor_sentences: list[dict[str, Any]],
                    latency: dict[str, Any] | None = None, tools: list[dict[str, Any]] | None = None,
                    usage: dict[str, Any] | None = None, student_extra: dict[str, Any] | None = None
                    ) -> None:
        """Append one turn in the speaking gap, off the event loop, and never raise.

        Every key of the §6b schema is always present — null where this path does not know the
        value — because the log is also the Anki mine and a consumer should never have to guess
        whether a missing key means "absent" or "an older writer".
        """
        self.turn += 1
        record = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "session": self.session_id,
            "persona": self.persona,
            "turn": self.turn,
            "student": {"text": student, "audio_ms": None, "stt_ms": None, **(student_extra or {})},
            "tutor": {"text": "".join(s.get("text", "") for s in tutor_sentences),
                      "sentences": tutor_sentences},
            "tools": tools or [],
            "latency": {"ttft_ms": None, "first_audio_ms": None, "first_play_ms": None,
                        "voice_to_voice_ms": None, **(latency or {})},
            "usage": usage or {},
        }
        line = json.dumps(record, ensure_ascii=False)
        path = self.log_path()

        def write() -> None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with _LOCK, path.open("a", encoding="utf-8") as f:
                    # One write of a complete line: a crash leaves every earlier line parseable.
                    f.write(line + "\n")
            except OSError:
                pass            # a lost turn record is a small loss; a raised error is not

        try:
            asyncio.get_running_loop().run_in_executor(None, write)
        except RuntimeError:     # no loop (tests, scripts): write inline
            write()

    # --------------------------------------------------------------- reading
    def recent_topics(self, sessions: int = RECENT_SESSIONS) -> list[str]:
        """Newest-first, de-duplicated topics from the last `sessions` summarised sessions."""
        rows = _read_jsonl(self.topics_jsonl)[-sessions:]
        seen: set[str] = set()
        out: list[str] = []
        for row in reversed(rows):
            for topic in row.get("topics", []):
                key = str(topic).strip()
                if key and key.lower() not in seen:
                    seen.add(key.lower())
                    out.append(key)
        return out

    def facts(self) -> tuple[list[str], list[str]]:
        """(what is true of the student, what this tutor has said about themselves).

        Two files, because they have different lifetimes: the student is the same person for
        every tutor, and a tutor's own life is theirs alone (user, 2026-09-12). Both are
        hand-editable and read forgivingly — anything that is not a "- " bullet is ignored, so
        the student can write themselves a comment.
        """
        return _bullets(self.about_md), _bullets(self.facts_md)

    def render(self) -> str:
        """The {{memory}} prompt section, or "" when there is nothing to remember yet.

        Empty renders to NOTHING — not an empty heading — so a first-ever session reads exactly
        like it did before memory existed (ROADMAP subsystem 18).
        """
        parts: list[str] = []
        say = memory_headings()
        student_facts, tutor_facts = self.facts()
        if student_facts or tutor_facts:
            known = [say["known"]]
            if student_facts:
                known.append(say["known_student"] + " " + "; ".join(_keep(student_facts, STUDENT_FACTS)))
            if tutor_facts:
                known.append(say["known_tutor"] + " " + "; ".join(_keep(tutor_facts, TUTOR_FACTS)))
            parts.append("\n".join(known))
        brief = _read(self.brief_md).strip()
        if brief:
            parts.append(say["brief"] + "\n" + brief)
        topics = self.recent_topics()
        if topics:
            parts.append(say["topics"] + " " + "、".join(topics))
        notes = _strip_comments(_read(self.student_md)).strip()
        if notes:
            parts.append(say["notes"] + "\n" + notes)
        return "\n\n".join(parts)

    # ------------------------------------------------------------ summarising
    def pending_logs(self) -> list[Path]:
        """This tutor's session logs with no topics row yet — i.e. never summarised.

        A lesson belongs to whoever taught it (user, 2026-09-12): another tutor must not read it
        back as their own. Logs written before personas were split carry no name, and are taken
        by whoever is teaching now — there was only one memory then.
        """
        # Keyed by (date, session), the two things a log's name carries and every topics row has
        # always written: a lesson that an older writer split across midnight is two files with
        # one session id, and the second must not read as done because the first is.
        done = {(str(row.get("date") or ""), str(row.get("session") or ""))
                for row in _read_jsonl(self.topics_jsonl)}
        pending = []
        for log in sorted(self.sessions.glob("*.jsonl")):
            session, date = log.stem.split("-", 3)[-1], log.stem[:10]
            if not session or (date, session) in done or session == self.session_id:
                continue
            if (who := _log_persona(log)) and self.persona and who != self.persona:
                continue
            pending.append(log)
        return pending

    def apply_summary(self, session: str, date: str, summary: dict[str, Any]) -> bool:
        """Write one summariser result. A malformed summary changes NOTHING.

        The previous brief and notes are only replaced by a result that parsed — a failed
        summarise must leave yesterday's memory intact rather than truncate it to nothing.
        """
        brief = str(summary.get("brief") or "").strip()
        if brief and self._brief_date() > date:
            brief = ""      # a catch-up of an older lesson must not replace a newer brief
        topics = [str(t).strip() for t in (summary.get("topics") or []) if str(t).strip()]
        notes_add = [str(n).strip() for n in (summary.get("notes") or []) if str(n).strip()]
        facts_add = {side: [str(f).strip()[:FACT_MAX_CHARS]
                            for f in (summary.get(f"{side}_facts") or []) if str(f).strip()]
                     for side in ("student", "tutor")}
        if not brief and not topics:
            return False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if brief:
                self.brief_md.write_text(f"({date}) {brief}\n", encoding="utf-8")
            if notes_add:
                existing = _read(self.student_md)
                if not existing.strip():
                    existing = ("<!-- Edit freely: this is what the tutor believes about you. A wrong "
                                "memory recalled confidently is worse than none. -->\n")
                have = existing.lower()
                fresh = [n for n in notes_add if n.lower() not in have]
                if fresh:
                    with self.student_md.open("w", encoding="utf-8") as f:
                        f.write(existing.rstrip() + "\n" + "".join(f"- {n}\n" for n in fresh))
            if facts_add["student"] or facts_add["tutor"]:
                self._merge_facts(facts_add["student"], facts_add["tutor"])
            with _LOCK, self.topics_jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"date": date, "session": session,
                                    "topics": topics[:TOPICS_PER_SESSION]},
                                   ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False

    def _merge_facts(self, student_add: list[str], tutor_add: list[str]) -> None:
        """Fold new facts into facts.md, in order, without repeating what is already there.

        Dedup is on the words, not the punctuation: the summariser says "Lives in Belgium" one
        week and "lives in Belgium." the next, and the tutor should not learn it twice.
        """
        student, tutor = self.facts()
        for have, add in ((student, student_add), (tutor, tutor_add)):
            seen = {_fact_key(f) for f in have}
            for fact in add:
                if (key := _fact_key(fact)) and key not in seen:
                    seen.add(key)
                    have.append(fact)
        if student_add:
            _write_facts(self.about_md, "# About the student",
                         "<!-- What your tutors know about you. Edit or delete any line: a wrong",
                         _keep(student, STUDENT_FACTS))
        if tutor_add:
            _write_facts(self.facts_md, f"# About {self.persona or 'the tutor'}, as the student knows them",
                         "<!-- What this tutor has said about themselves. Edit or delete any line: a wrong",
                         _keep(tutor, TUTOR_FACTS))

    def mark_done(self, session: str, date: str) -> None:
        """Record a session as summarised without changing any memory — for one with nothing in
        it. The topics row IS the bookkeeping (there is no second file), so an empty row is how a
        log stops being pending."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with _LOCK, self.topics_jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"date": date, "session": session, "topics": []}) + "\n")
        except OSError:
            pass

    def _brief_date(self) -> str:
        """The date of the brief on disk — it is written as "(YYYY-MM-DD) …"."""
        head = _read(self.brief_md)[:12]
        return head[1:11] if head.startswith("(") else ""

    async def summarise_pending(self, ask: Callable[[str], Awaitable[str]], instructions: str,
                                limit: int | None = None,
                                on_log: Callable[[Path, int, int], None] | None = None) -> int:
        """Summarise unsummarised logs. `ask(prompt) -> reply text`. Returns how many landed.

        `ask` is injected rather than constructed here so tests can drive this with a fake, and so
        the caller decides which (cheap) model spends the tokens. `limit` takes the newest logs
        first — the launch summarises the last lesson, which is the one the tutor greets with, and the
        older ones are caught up in the background while the student is already talking (2026-09-12:
        five pending sessions held the launch for 80 s). `on_log` reports progress.
        """
        pending = self.pending_logs()
        queue = list(reversed(pending))[:limit] if limit else pending
        landed = 0
        for at, log in enumerate(queue, start=1):
            if on_log is not None:
                on_log(log, at, len(queue))
            excerpt = excerpt_of(log)
            if not has_a_lesson(excerpt):
                # A launch where the student never spoke: she greeted, nobody answered. There is
                # nothing to remember, and asking a model to say so costs the CLI's cold start
                # every launch from then on (2026-09-12: the newest pending log was 73 chars and
                # the launch reported "0 summarised"). Mark it done so it is never retried.
                self.mark_done(log.stem.split("-", 3)[-1], log.stem[:10])
                continue
            try:
                reply = await ask(instructions + "\n\n" + excerpt)
            except Exception:           # noqa: BLE001 - best-effort by contract
                continue
            summary = parse_summary(reply)
            session, date = log.stem.split("-", 3)[-1], log.stem[:10]
            if summary is None:
                # It answered, but not in JSON. That is the model, not the lesson, and it will do
                # the same next launch — so stop asking. Only a *failed call* (the except above)
                # stays pending, because that one is worth retrying.
                self.mark_done(session, date)
                continue
            if self.apply_summary(session, date, summary):
                landed += 1
            else:
                # It answered, and its answer was "there is nothing here" (an empty brief and no
                # topics). Retrying that every launch is what made five old sessions cost a model
                # call every time the app started (user, 2026-09-12). One answer is enough.
                self.mark_done(session, date)
        return landed


# ---------------------------------------------------------------------- helpers
def memory_headings(path: Path = HEADINGS_FILE) -> dict[str, str]:
    """The `## key` blocks of prompts/memory.md, comments stripped. A key the file lacks renders
    as the key itself in capitals — a visible placeholder, never a silent blank."""
    out: dict[str, str] = {}
    key = None
    for line in _strip_comments(_read(path)).splitlines():
        if line.startswith("## "):
            key = line[3:].strip()
            out[key] = ""
        elif key is not None and line.strip():
            out[key] = (out[key] + " " + line.strip()).strip()
    return _Headings(out)


class _Headings(dict):
    def __missing__(self, key: str) -> str:
        return str(key).upper()


def has_a_lesson(excerpt: str) -> bool:
    """Whether a transcript is worth a summariser call: the student has to have said something.
    Her own greeting to an empty room is not a lesson."""
    return any(line.startswith("STUDENT: ") and line[9:].strip() for line in excerpt.splitlines())


def launch_stamp() -> str:
    """Now, in the form every turn record's `ts` has — the `since` a handoff is built from."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def excerpt_of(log: Path, limit: int = EXCERPT_MAX_CHARS, since: str | None = None) -> str:
    """Text-only transcript of one session, newest turns kept if it must be cut.

    `since` (a `launch_stamp()`) keeps only the turns recorded from that moment on: a rotation
    handoff must carry THIS launch's lesson, not an earlier one that happens to share the log —
    told "do not greet again" over a lesson that ended hours ago, a replacement spawned before the
    first turn never greeted at all (2026-09-12). Empty when nothing has happened since.
    """
    lines: list[str] = []
    for row in _read_jsonl(log):
        if since and str(row.get("ts") or "") < since:
            continue
        student = (row.get("student") or {}).get("text") or ""
        tutor = (row.get("tutor") or {}).get("text") or ""
        if student:
            lines.append(f"STUDENT: {student}")
        if tutor:
            lines.append(f"TUTOR: {tutor}")
    text = "\n".join(lines)
    return text[-limit:] if len(text) > limit else text


def parse_summary(reply: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply, or None. Models wrap JSON in prose."""
    match = re.search(r"\{.*\}", reply or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _fact_key(fact: str) -> str:
    """What makes two statements of the same fact the same: the letters, lowercased."""
    return re.sub(r"[^0-9a-z぀-ヿ一-鿿]+", "", fact.lower())


def _keep(facts: list[str], cap: int) -> list[str]:
    """At most `cap`, oldest first — but always the newest `FACTS_FRESH`.

    Dropping the oldest would lose the student's name to a week of small talk; keeping only the
    oldest would freeze the memory the day the list fills up. So: the anchors, then what is new.
    """
    if len(facts) <= cap:
        return facts
    return facts[:max(0, cap - FACTS_FRESH)] + facts[-FACTS_FRESH:]


def _log_persona(path: Path) -> str:
    """Who taught the lesson in this log, from its first turn; "" for a log that predates the
    per-tutor split or cannot be read."""
    try:
        with path.open(encoding="utf-8") as f:
            return str(json.loads(f.readline() or "{}").get("persona") or "")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""


def _bullets(path: Path) -> list[str]:
    """The "- " lines of a facts file, trimmed, comments and headings ignored."""
    out = []
    for line in _strip_comments(_read(path)).splitlines():
        if line.strip().startswith("- ") and (fact := line.strip()[2:].strip()[:FACT_MAX_CHARS]):
            out.append(fact)
    return out


def _write_facts(path: Path, heading: str, first_comment_line: str, facts: list[str]) -> None:
    lines = [first_comment_line,
             "     memory recalled confidently is worse than none. The oldest lines survive when",
             "     the list is trimmed, so what was learned first stays. -->",
             heading, *(f"- {f}" for f in facts)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every complete line that parses. A half-written last line is skipped, not fatal."""
    out = []
    for line in _read(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)
