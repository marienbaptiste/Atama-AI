"""Tiny disk cache for SRS fetch results (spec §5: 1 h TTL; `stale` is a first-class state)."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def cache_load(path: Path, ttl_s: float, now: float | None = None) -> tuple[Any | None, bool]:
    """Return (data, fresh). data is None if there is no usable cache."""
    if not path.is_file():
        return None, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None, False
    age = (now if now is not None else time.time()) - path.stat().st_mtime
    return data, age <= ttl_s


def cache_store(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cache-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
