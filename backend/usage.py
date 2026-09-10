"""This app's Claude use per calendar month, for the status bar (user request, 2026-09-10).

In API-equivalent dollars. The account is a subscription, so nothing here is a bill: it is the
CLI's own `total_cost_usd` — what the same turns would have cost on the API — which is a fair
measure of how much the app uses. No monthly figure is reported by the CLI (only the five-hour
window, see constants.py), so this ledger keeps one.

The CLI reports cost CUMULATIVELY per session (verified with a two-turn probe, constants.py), so
the ledger is given each turn's session total and adds the difference. A total lower than the
last one means a new session (a restart, a tutor switch), which counts in full.

Stored as CACHE_DIR/usage/<YYYY-MM>.json: generated and personal, never committed. Best-effort:
a damaged file starts the month again rather than stopping a lesson.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class Ledger:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self._last_total: float | None = None

    def path(self, when: dt.date | None = None) -> Path:
        return self.dir / f"{(when or dt.date.today()):%Y-%m}.json"

    def month(self, when: dt.date | None = None) -> dict[str, Any]:
        try:
            data = json.loads(self.path(when).read_text(encoding="utf-8"))
            return {"cost_usd": float(data.get("cost_usd", 0.0)), "turns": int(data.get("turns", 0))}
        except (OSError, ValueError, TypeError, AttributeError):
            return {"cost_usd": 0.0, "turns": 0}

    def new_session(self) -> None:
        """The brain was replaced (tutor switch): its first total counts in full."""
        self._last_total = None

    def add_session_total(self, total: float | None, when: dt.date | None = None) -> dict[str, Any]:
        """Record one turn, given the SESSION's cumulative cost after it. Returns the month so far."""
        if total is None:
            return self.month(when)
        new_session = self._last_total is None or total < self._last_total
        delta = total if new_session else total - self._last_total
        self._last_total = total
        data = self.month(when)
        data = {"cost_usd": round(data["cost_usd"] + max(0.0, delta), 6), "turns": data["turns"] + 1}
        self._write(self.path(when), data)
        return data

    @staticmethod
    def _write(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".usage-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
