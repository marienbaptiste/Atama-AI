"""Start everything, in one command.

    python -m backend.tools.up          # or: make run

Brings up the containers, waits until VOICEVOX can actually answer, then runs the tutor with the
browser avatar and opens it. Ctrl+C stops the tutor and leaves the containers running, because
they are slow to start and cheap to keep; `make stop` is the one command that takes them down.

Waiting is the point of this file. `docker compose up -d` returns as soon as the container is
created, not when the engine inside it is listening, so starting the app immediately means the
first thing you see is "VOICEVOX is not running" (observed 2026-09-10: 14 s from restart to
first 200).
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from backend import config


def compose(*args: str) -> int:
    """Run docker compose, or explain why we cannot."""
    docker = shutil.which("docker")
    if not docker:
        print("docker is not on PATH — start VOICEVOX yourself, or install Docker Desktop",
              file=sys.stderr)
        return 127
    return subprocess.call([docker, "compose", *args], cwd=str(config.REPO_ROOT))


def wait_for_voicevox(url: str, seconds: float = 90.0) -> bool:
    """Poll until the engine answers. Never sleep a fixed guess (ROADMAP findings)."""
    deadline = time.monotonic() + seconds
    first = True
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/version", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            if first:
                print(f"waiting for VOICEVOX at {url} …", flush=True)
                first = False
        time.sleep(0.5)
    return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="make run", description="start everything")
    ap.add_argument("--no-docker", action="store_true", help="assume the containers are already up")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser")
    ap.add_argument("--text", action="store_true", help="text REPL only: no mic, no avatar")
    args, rest = ap.parse_known_args(argv[1:])
    cfg = config.load()

    if not args.no_docker and compose("up", "-d") not in (0, 127):
        print("docker compose up failed", file=sys.stderr)
        return 1

    if not args.text and not wait_for_voicevox(str(cfg.VOICEVOX_URL)):
        print(f"VOICEVOX never answered at {cfg.VOICEVOX_URL}. `docker compose logs voicevox`",
              file=sys.stderr)
        return 1

    from backend import repl
    argv_repl = ["repl", *rest] if args.text else [
        "repl", "--listen", "--browser", *([] if args.no_open else ["--show"]), *rest]
    parsed = repl.parse(argv_repl[1:])
    try:
        return asyncio.run(repl.run(parsed))
    except KeyboardInterrupt:
        print("\nstopped. The containers are still up — `make stop` to take them down.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
