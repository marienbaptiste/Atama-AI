"""`make check-secrets`: no token-shaped strings and no current secret values in the tracked tree."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from backend import config

PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"Bearer [A-Za-z0-9_-]{24,}"),
    re.compile(r"Token token=[A-Za-z0-9_-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,}"),  # JWT
]


def tracked_files(root: Path) -> list[Path]:
    res = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=root, capture_output=True, text=True, check=False)
    return [root / line for line in res.stdout.splitlines() if line]


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    secrets = list(config.load().secrets().values())
    hits: list[str] = []
    for path in tracked_files(root):
        if not path.is_file() or path.suffix in {".glb", ".png", ".jpg", ".wav", ".bin"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if any(p.search(line) for p in PATTERNS) or any(s in line for s in secrets):
                hits.append(f"{path.relative_to(root).as_posix()}:{i}")
    if hits:
        print("check-secrets: possible secret in tracked/untracked-unignored files:", file=sys.stderr)
        for h in hits:
            print("  " + h, file=sys.stderr)
        return 1
    print("check-secrets: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
