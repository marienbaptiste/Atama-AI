r"""Start everything, in one command.

    python -m backend.tools.up          # or: make run

Brings up the containers, waits until VOICEVOX can actually answer, then runs the tutor with the
browser avatar and opens it. Ctrl+C stops the tutor and leaves the containers running, because
they are slow to start and cheap to keep; `make stop` (or `.\stop` on Windows) takes them down —
and so does the page's stop button, which means "I am done for today" (user, 2026-09-12).

Waiting is the point of this file. `docker compose up -d` returns as soon as the container is
created, not when the engine inside it is listening, so starting the app immediately means the
first thing you see is "VOICEVOX is not running" (observed 2026-09-10: 14 s from restart to
first 200).

Failing early is the other point: a docker that is missing or not running used to read as
success (rc 127) and send the launch into a 90 s wait for a VOICEVOX nothing had started; a
port already taken by a previous tutor used to surface as uvicorn's traceback. Both are now one
line each, before anything starts.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import socket
import stat
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


def searxng_secret() -> str:
    """The secret compose needs for SearXNG, generated once and kept out of the repo.

    It used to live in `.env`, which meant a fresh clone could not start until someone created
    that file by hand — and there is no `.env` any more (ADR-022 amendment). It signs CSRF tokens
    and the image proxy of a loopback-only search container, so it is machine-local state, not
    configuration: it lives beside the other per-user state, mode 0600, and is generated the
    first time anything starts. Regenerating it only invalidates open search sessions.
    """
    path = config.claude_cwd(config.load()).parent / "searxng-secret"
    try:
        if (existing := path.read_text(encoding="utf-8").strip()):
            return existing
    except OSError:
        pass
    secret = secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret + "\n", encoding="utf-8")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass                    # a secret we cannot store still starts this run
    return secret


def docker_problem(*, which=shutil.which, run=subprocess.run) -> str | None:
    """Why the containers cannot be started, or None. Checked BEFORE `compose up`, because a
    missing or stopped docker otherwise turns into a 90 s wait for a VOICEVOX nobody started."""
    docker = which("docker")
    if not docker:
        return ("docker is not on PATH. Install Docker Desktop, or start VOICEVOX and SearXNG yourself and "
                f"launch with `{RUN_HINT} --no-docker`.")
    try:
        done = run([docker, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return f"docker is installed but `docker info` could not run ({type(e).__name__}). Start Docker Desktop."
    if done.returncode != 0:
        tail = (done.stderr or "").strip().splitlines()
        return ("docker is installed but its engine is not running (`docker info` failed"
                + (f": {tail[-1][:120]}" if tail else "") + "). Start Docker Desktop, then try again.")
    return None


def compose(*args: str) -> int:
    """Run docker compose, or explain why we cannot."""
    docker = shutil.which("docker")
    if not docker:
        print("docker is not on PATH — start VOICEVOX yourself, or install Docker Desktop",
              file=sys.stderr)
        return 127
    env = {**os.environ, "SEARXNG_SECRET": os.environ.get("SEARXNG_SECRET") or searxng_secret()}
    return subprocess.call([docker, "compose", *args], cwd=str(config.REPO_ROOT), env=env)


def port_free(host: str, port: int) -> bool:
    """Can the app bind its port right now? A bind probe, released at once: uvicorn's own failure
    is a traceback deep in the launch, and the usual cause is a tutor still running."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, int(port)))
            return True
    except OSError:
        return False


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


def ensure_frontend() -> bool:
    """Build the page when it is missing or older than its source (ADR-009).

    False when there is no page to open at all: the launcher then stops and says why, rather than
    open a browser tab on nothing. A failed REbuild keeps the previous build, and says so."""
    built = FRONTEND / "dist" / "index.html"
    sources = [FRONTEND / "index.html", FRONTEND / "package.json", *(FRONTEND / "src").rglob("*")]
    newest = max((p.stat().st_mtime for p in sources if p.is_file()), default=0.0)
    if built.exists() and built.stat().st_mtime >= newest:
        return True
    npm = shutil.which("npm")
    if not npm:
        return _without_build(built, "npm is not on PATH - install Node.js 20+ (README: Prerequisites)")
    if not (FRONTEND / "node_modules").exists():
        print("installing the page's build tools (once) …", flush=True)
        if subprocess.call([npm, "ci"], cwd=str(FRONTEND)) != 0:
            return _without_build(built, "npm ci failed (see above)")
    print("building the page …", flush=True)
    if subprocess.call([npm, "run", "build"], cwd=str(FRONTEND)) != 0:
        return _without_build(built, "the page did not build (see above)")
    return True


def _without_build(built, why: str) -> bool:
    if built.exists():
        print(f"{why} - opening the previous build of the page", file=sys.stderr)
        return True
    print(f"{why} - and there is no earlier build to open. Fix that, then {RUN_HINT} again.",
          file=sys.stderr)
    return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="make run", description="start everything")
    ap.add_argument("--no-docker", action="store_true", help="assume the containers are already up")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser")
    ap.add_argument("--text", action="store_true", help="text REPL only: no mic, no avatar")
    args, rest = ap.parse_known_args(argv[1:])
    cfg = config.load()

    if not args.no_docker:
        if (problem := docker_problem()):
            print(problem, file=sys.stderr)
            return 1
        if compose("up", "-d") != 0:
            print("docker compose up failed", file=sys.stderr)
            return 1

    if not args.text and not port_free(str(cfg.HOST), int(cfg.PORT)):
        print(f"port {cfg.PORT} on {cfg.HOST} is already in use — a tutor still running in another window? "
              "Stop it (Ctrl+C there, or the page's stop button), or change PORT in settings → Advanced.",
              file=sys.stderr)
        return 1

    if not args.text and not wait_for_voicevox(str(cfg.VOICEVOX_URL)):
        print(f"VOICEVOX never answered at {cfg.VOICEVOX_URL}. `docker compose logs voicevox`",
              file=sys.stderr)
        return 1

    if not args.text and not ensure_frontend():
        return 1

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
    try:
        raise SystemExit(main(sys.argv))
    except config.ConfigError as exc:      # a malformed settings.json: one line, not a traceback
        sys.exit(str(exc))
