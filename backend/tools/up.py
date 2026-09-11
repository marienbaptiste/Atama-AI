r"""Start everything, in one command.

    python -m backend.tools.up          # or: make run

Brings up the containers, waits until VOICEVOX can actually answer, then runs the tutor with the
browser avatar and opens it. Ctrl+C stops the tutor and leaves the containers running, because
they are slow to start and cheap to keep; `make stop` (or `.\stop` on Windows) takes them down.

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

#: What to tell the user to type. `make` does not exist on Windows, which is the platform this
#: is developed on, so printing it there sends them to a CommandNotFoundException (2026-09-10).
STOP_HINT = r".\stop" if sys.platform == "win32" else "make stop"
RUN_HINT = r".\run" if sys.platform == "win32" else "make run"


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


FRONTEND = config.REPO_ROOT / "frontend"


def ensure_frontend() -> None:
    """Build the page when it is missing or older than its source (ADR-009).

    Without Node.js the prototype page still works, so this warns rather than fails — the tutor
    matters more than the page it appears on."""
    built = FRONTEND / "dist" / "index.html"
    sources = [FRONTEND / "index.html", FRONTEND / "package.json", *(FRONTEND / "src").rglob("*")]
    newest = max((p.stat().st_mtime for p in sources if p.is_file()), default=0.0)
    if built.exists() and built.stat().st_mtime >= newest:
        return
    npm = shutil.which("npm")
    if not npm:
        print("npm is not on PATH - opening the prototype page. Install Node.js for the full page.",
              file=sys.stderr)
        return
    if not (FRONTEND / "node_modules").exists():
        print("installing the page's build tools (once) …", flush=True)
        if subprocess.call([npm, "ci"], cwd=str(FRONTEND)) != 0:
            print("npm ci failed - opening the prototype page", file=sys.stderr)
            return
    print("building the page …", flush=True)
    if subprocess.call([npm, "run", "build"], cwd=str(FRONTEND)) != 0:
        print("the page did not build (see above) - opening the last build or the prototype",
              file=sys.stderr)


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

    if not args.text:
        ensure_frontend()

    from backend import repl
    argv_repl = ["repl", *rest] if args.text else [
        "repl", "--listen", "--browser", *([] if args.no_open else ["--show"]), *rest]
    parsed = repl.parse(argv_repl[1:])
    try:
        return asyncio.run(repl.run(parsed))
    except KeyboardInterrupt:
        print(f"\nstopped. The containers are still up — `{STOP_HINT}` takes them down.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
