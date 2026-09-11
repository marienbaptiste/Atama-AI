"""How often she tags a turn with an emotion — ROADMAP 17, gate M3f (tags in >= 30 % of turns).

The turn log (spec §6b) records every sentence she spoke with the emotion it was played with, so
the rate is read from real lessons rather than estimated. If it stays low, the prompt wording is
what needs changing, not the pipeline (ROADMAP 17).

    .venv/Scripts/python -m backend.tools.emotion_rate            # every logged session
    .venv/Scripts/python -m backend.tools.emotion_rate --last 3   # the three latest
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from backend import config
from backend import memory as memory_api

#: Gate M3f.
GATE = 0.30


def read(path: Path) -> list[dict[str, Any]]:
    """Every whole record in one session log. A half-written last line is skipped, not fatal."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def rate(records: Iterable[dict[str, Any]]) -> tuple[int, int, Counter]:
    """(turns with at least one tagged sentence, turns she spoke in, emotions counted by sentence)."""
    tagged = spoken = 0
    seen: Counter = Counter()
    for record in records:
        sentences = (record.get("tutor") or {}).get("sentences") or []
        if not sentences:
            continue
        spoken += 1
        emotions = [s.get("emotion") for s in sentences if isinstance(s, dict) and s.get("emotion")]
        seen.update(emotions)
        tagged += bool(emotions)
    return tagged, spoken, seen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="emotion_rate", description=__doc__.split("\n\n")[0])
    ap.add_argument("--last", type=int, default=0, help="only the N most recent sessions")
    args = ap.parse_args(argv)
    folder = memory_api.sessions_dir(config.load())
    files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if args.last:
        files = files[-args.last:]
    if not files:
        print(f"no session logs in {folder}")
        return 1
    tagged = spoken = 0
    seen: Counter = Counter()
    for path in files:
        t, n, e = rate(read(path))
        tagged, spoken = tagged + t, spoken + n
        seen.update(e)
        if n:
            print(f"{path.name}: {t}/{n} turns tagged ({t / n:.0%})")
    if not spoken:
        print("no spoken turns logged yet")
        return 1
    share = tagged / spoken
    print(f"\noverall: {tagged}/{spoken} turns tagged ({share:.0%}) - gate M3f needs {GATE:.0%}: "
          + ("MET" if share >= GATE else "NOT MET"))
    if seen:
        print("by sentence: " + ", ".join(f"{k} {v}" for k, v in seen.most_common()))
    return 0 if share >= GATE else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
