"""Pre-emptive session rotation (ADR-032, spec §6b, ROADMAP subsystem 18).

A long lesson fills the model's window, and the provider then compacts on its own schedule —
seconds of silence mid-lesson, arriving exactly when the lesson has gone well long enough to fill a
window. The rotator sees it coming from the context meter the brain already reports after every
turn (`brain.meters`, verified fields in constants.py) and replaces the session first:

  observe(brain)  after a turn: arms at CONTEXT_ROTATE_AT x the model's window.
  prepare()       starts the replacement in the background, with a handoff built from the lesson's
                  own turn log (deterministic, no model call). Nothing waits on it.
  take(current)   at a turn boundary, never inside one: the replacement if it is ready, else None —
                  the old session carries on and the next gap tries again. Rotation is never the
                  reason a turn is slow.
  settle()        after the new session has taken a turn: only then is the old one closed.

Where the provider compacts is its own policy: no command reports it and it can change without a
release of ours (verified 2026-09-11, constants.py). So the threshold is a fraction of the window
the provider itself reports, never a measured token count, and `learn()` lowers it — persisted in
CACHE_DIR/compaction.json — the first time the provider compacts on its own before we rotated.

A replacement that fails to start keeps the old session; after MAX_FAILURES the rotator stops
trying for the session and says so, and the provider's own compaction is the fallback — which the
§10 instrumentation then shows as the slow turn it is. CONTEXT_ROTATE_AT = 0 never rotates, a
supported configuration (spec §12 M4).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

#: Replacements that fail to start this many times running are not tried again this session.
MAX_FAILURES = 3
#: After the provider compacts on its own at N tokens, rotate at this fraction of N from then on.
LEARN_MARGIN = 0.85
#: A learned threshold never goes below this: rotating every few turns would be its own problem.
MIN_LEARNED = 0.1
#: Where a learned threshold is kept, under CACHE_DIR.
LEARNED_FILE = "compaction.json"


def load_learned(path: Path) -> float | None:
    """The threshold learned from an earlier session, or None. A bad file is no file."""
    try:
        value = float(json.loads(path.read_text(encoding="utf-8"))["rotate_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return value if 0 < value < 1 else None


def save_learned(path: Path, rotate_at: float, **why: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rotate_at": rotate_at, **why}, indent=2), encoding="utf-8")


def effective_threshold(configured: float, learned: float | None) -> float:
    """The configured fraction, lowered by a learned one. 0 stays 0: never is the user's call."""
    configured = float(configured or 0.0)
    if configured <= 0 or not learned:
        return configured
    return min(configured, learned)


class Rotator:
    """`spawn(handoff)` builds AND starts the replacement, and it owns the process until it
    returns: if it is cancelled (discard() during a persona switch or a resync) or fails after
    the process exists, it must close that process before the exception leaves it — the rotator
    only ever holds brains that finished starting."""

    def __init__(self, threshold: float, spawn: Callable[[str], Awaitable[Any]],
                 handoff: Callable[[], str], log: Callable[[str], None] = lambda s: None) -> None:
        self.threshold = float(threshold or 0.0)
        self.spawn = spawn
        self.handoff = handoff
        self.log = log
        self.armed = False
        self.failures = 0
        self.rotations = 0
        self._task: asyncio.Task | None = None
        self._ready: Any = None
        self._retiring: Any = None
        #: Bumped by discard(): a replacement that finishes starting after it was discarded is
        #: closed rather than kept — it was built for a tutor, or a profile, that no longer applies.
        self._generation = 0

    @property
    def enabled(self) -> bool:
        return self.threshold > 0 and self.failures < MAX_FAILURES

    def observe(self, brain: Any) -> bool:
        """After a turn: arm when her context has reached the threshold. Returns `armed`."""
        if not self.enabled or self.armed or self._ready is not None:
            return self.armed
        meters = getattr(brain, "meters", None) or {}
        used, window = meters.get("context_tokens"), meters.get("context_window")
        if used and window and used >= self.threshold * window:
            self.armed = True
            self.log(f"context {used:,} of {window:,} tokens (>= {self.threshold:.0%}): rotation armed")
        return self.armed

    def learn(self, trigger: str, pre_tokens: int | None, window: int | None) -> float | None:
        """The provider compacted on its own before we rotated: its policy is earlier than our
        threshold. Rotate earlier from now on. Returns the new threshold, or None if unchanged.

        Ignores a compaction someone asked for ("manual"), and never switches rotation on — a
        threshold of 0 is the user saying never."""
        if trigger != "auto" or self.threshold <= 0 or not pre_tokens or not window:
            return None
        lowered = round(max(MIN_LEARNED, LEARN_MARGIN * pre_tokens / window), 3)
        if lowered >= self.threshold:
            return None
        self.threshold = lowered
        self.log(f"the provider compacted on its own at {pre_tokens:,} of {window:,} tokens: "
                 f"rotating at {lowered:.0%} from now on")
        return lowered

    def prepare(self) -> None:
        """Start the replacement in the background, once. Never awaited by a turn."""
        if not self.armed or self._task is not None or self._ready is not None:
            return
        self._task = asyncio.get_running_loop().create_task(self._prepare(self._generation))

    async def _prepare(self, generation: int) -> None:
        try:
            brain = await self.spawn(self.handoff())
        except asyncio.CancelledError:
            # discard() cancelled us mid-spawn. The half-built brain is `spawn`'s to close — the
            # rotator never saw it — which is why the contract on `spawn` (class doc) says a
            # cancelled start closes what it launched. Nothing to keep; nothing to retry.
            raise
        except Exception as exc:  # noqa: BLE001 - the old session carries on; say why and retry
            self.failures += 1
            self.log(f"replacement did not start ({type(exc).__name__}: {exc}); keeping the current "
                     f"session, attempt {self.failures} of {MAX_FAILURES}")
            if self.failures >= MAX_FAILURES:
                self.armed = False
                self.log("giving up for this session: the provider will compact on its own - "
                         "expect one slow turn, logged as a latency event")
            return
        finally:
            self._task = None
        if generation != self._generation:
            with contextlib.suppress(Exception):
                await brain.aclose()
            return
        self._ready = brain
        self.log("replacement ready - it takes over at the next turn")

    async def rotate_next_turn(self, reason: str) -> None:
        """Rotate at the next turn boundary whatever the context size — e.g. the study profile was
        refreshed and she should have the new one (spec §5b resync). Works with automatic rotation
        off; still built in the background, still swapped only between turns.

        Not called `request`: the Golden Rule gate reads `.request(` in any module that touches
        backend.srs as a possible HTTP write (spec §0) — and this is no request to anyone."""
        await self.discard()             # a replacement built on the old profile would be stale
        self.armed = True
        self.failures = 0
        self.log(f"{reason}: a fresh session takes over at the next turn")
        self.prepare()

    def take(self, current: Any) -> Any:
        """At a turn boundary: the ready replacement (now authoritative), or None."""
        new, self._ready = self._ready, None
        if new is None:
            return None
        self._retiring = current
        self.armed = False
        self.failures = 0
        self.rotations += 1
        self.log(f"rotated to a fresh session (rotation {self.rotations})")
        return new

    async def settle(self) -> None:
        """After the replacement has taken a turn: close the session it replaced (ADR-032)."""
        old, self._retiring = self._retiring, None
        if old is not None:
            with contextlib.suppress(Exception):
                await old.aclose()

    async def discard(self) -> None:
        """Drop any pending replacement — the tutor or the profile changed underneath it."""
        self._generation += 1
        self.armed = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        ready, self._ready = self._ready, None
        if ready is not None:
            with contextlib.suppress(Exception):
                await ready.aclose()

    async def aclose(self) -> None:
        await self.discard()
        await self.settle()
