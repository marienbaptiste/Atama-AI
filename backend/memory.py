"""Cross-session memory (spec §6b, ADR-031).

Four tiers, each with exactly one read moment and one write moment, none on the critical path:

    tier                where                                 read               written
    turn log            logs/sessions/<date>-<session>.jsonl  never by the tutor in the speaking gap
    student notes       <state>/memory/student.md             session start      after summarising
    last-session brief  <state>/memory/last-session.md        session start      after summarising
    recent topics       <state>/memory/topics.jsonl           session start      after summarising

The fourth tier is the one that keeps lessons from repeating themselves: a few short noun phrases
per session, read at start as "recently discussed — open on something else" (2026-09-10).

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from backend import config

#: How many past sessions' topics to show. Enough to stop a week of lessons opening on the same
#: typhoon; few enough to stay a single short line in the prompt.
RECENT_SESSIONS = 8
#: Topics kept per session by the summariser. Short noun phrases, not sentences.
TOPICS_PER_SESSION = 5
#: The summariser reads a text-only excerpt of the log, capped so a long lesson cannot make the
#: once-per-session summary expensive.
EXCERPT_MAX_CHARS = 6000

_LOCK = threading.Lock()


def memory_dir(cfg) -> Path:
    """<state>/memory — beside the claude cwd, which config already guarantees is outside the
    repository (a memory file inside it could be committed, and it holds the student's life)."""
    return config.claude_cwd(cfg).parent / "memory"


def sessions_dir(cfg) -> Path:
    return cfg.path("LOG_DIR") / "sessions"


@dataclass
class Memory:
    root: Path              # <state>/memory
    sessions: Path          # logs/sessions
    session_id: str = ""
    turn: int = 0

    @classmethod
    def from_config(cls, cfg, session_id: str = "") -> "Memory":
        return cls(root=memory_dir(cfg), sessions=sessions_dir(cfg), session_id=session_id)

    # ------------------------------------------------------------------ paths
    @property
    def student_md(self) -> Path:
        return self.root / "student.md"

    @property
    def brief_md(self) -> Path:
        return self.root / "last-session.md"

    @property
    def topics_jsonl(self) -> Path:
        return self.root / "topics.jsonl"

    def log_path(self) -> Path:
        return self.sessions / f"{dt.date.today().isoformat()}-{self.session_id}.jsonl"

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
            "turn": self.turn,
            "student": {"text": student, "audio_ms": None, "stt_ms": None, **(student_extra or {})},
            "tutor": {"text": "".join(s.get("text", "") for s in tutor_sentences),
                      "sentences": tutor_sentences},
            "tools": tools or [],
            "latency": {"ttft_ms": None, "first_audio_ms": None, "voice_to_voice_ms": None,
                        **(latency or {})},
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

    def render(self) -> str:
        """The {{memory}} prompt section, or "" when there is nothing to remember yet.

        Empty renders to NOTHING — not an empty heading — so a first-ever session reads exactly
        like it did before memory existed (ROADMAP subsystem 18).
        """
        parts: list[str] = []
        brief = _read(self.brief_md).strip()
        if brief:
            parts.append("LAST SESSION\n" + brief)
        topics = self.recent_topics()
        if topics:
            parts.append("RECENTLY DISCUSSED — do not open on these; follow one up only if the "
                         "student raises it: " + "、".join(topics))
        notes = _strip_comments(_read(self.student_md)).strip()
        if notes:
            parts.append("ABOUT THE STUDENT\n" + notes)
        return "\n\n".join(parts)

    # ------------------------------------------------------------ summarising
    def pending_logs(self) -> list[Path]:
        """Session logs with no topics row yet — i.e. never summarised."""
        done = {row.get("session") for row in _read_jsonl(self.topics_jsonl)}
        pending = []
        for log in sorted(self.sessions.glob("*.jsonl")):
            session = log.stem.split("-", 3)[-1]
            if session and session not in done and session != self.session_id:
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
            with _LOCK, self.topics_jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"date": date, "session": session,
                                    "topics": topics[:TOPICS_PER_SESSION]},
                                   ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False

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
        first — the launch summarises the last lesson, which is the one she greets with, and the
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
            if not excerpt:
                continue
            try:
                reply = await ask(instructions + "\n\n" + excerpt)
            except Exception:           # noqa: BLE001 - best-effort by contract
                continue
            summary = parse_summary(reply)
            if summary is None:
                continue
            date = log.stem[:10]
            if self.apply_summary(log.stem.split("-", 3)[-1], date, summary):
                landed += 1
        return landed


# ---------------------------------------------------------------------- helpers
def excerpt_of(log: Path, limit: int = EXCERPT_MAX_CHARS) -> str:
    """Text-only transcript of one session, newest turns kept if it must be cut."""
    lines: list[str] = []
    for row in _read_jsonl(log):
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
