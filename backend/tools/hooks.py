"""`make hooks`: install a pre-commit hook running the readonly gate and the secret check."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

HOOK = """#!/bin/sh
# atama-AI pre-commit: Golden Rule gate (spec §0) + secret scan. No bypass.
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=.venv/Scripts/python; fi
$PY -m backend.tools.readonly_gate || exit 1
$PY -m backend.tools.check_secrets || exit 1
"""


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    hooks_dir = root / ".git" / "hooks"
    if not hooks_dir.is_dir():
        print("hooks: not a git repo", file=sys.stderr)
        return 1
    path = hooks_dir / "pre-commit"
    path.write_text(HOOK, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"hooks: installed {path.relative_to(root).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
