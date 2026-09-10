"""How much the app talks to Claude, per calendar month — for the status bar (user request, 2026-09-10).

Counted in TURNS. The account is a subscription driven through `claude -p`: there is no bill, and
the CLI reports no monthly figure — only whether the five-hour usage window still allows requests
(rate_limit_event, constants.py). Dollars were shown first (the CLI's API-equivalent
`total_cost_usd`) and made no sense for a subscription (user, same day), so the honest monthly
number is how many turns the app has had.

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

    def path(self, when: dt.date | None = None) -> Path:
        return self.dir / f"{(when or dt.date.today()):%Y-%m}.json"

    def month(self, when: dt.date | None = None) -> dict[str, Any]:
        try:
            data = json.loads(self.path(when).read_text(encoding="utf-8"))
            return {"turns": int(data.get("turns", 0))}
        except (OSError, ValueError, TypeError, AttributeError):
            return {"turns": 0}

    def add_turn(self, when: dt.date | None = None) -> dict[str, Any]:
        """One more turn with her this month. Returns the month so far."""
        data = {"turns": self.month(when)["turns"] + 1}
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
