"""A model tier resolves to the NEWEST model the CLI accepts (user request, 2026-09-10).

Verified 2026-09-10 (Claude CLI 2.1.159), one tiny turn each:
  * the bare aliases lag behind the releases: `sonnet` ran claude-sonnet-4-6 and `opus` ran
    claude-opus-4-8, while claude-sonnet-5 and claude-opus-5 both work on this account;
    `haiku` ran claude-haiku-4-5-20251001;
  * an id the account cannot use still STARTS (system/init echoes it back) and fails on its first
    turn: result.is_error, "There's an issue with the selected model (...). It may not exist or
    you may not have access to it.", exit 1.

The CLI cannot list models, so the candidates live in backend/data/model_tiers.txt, newest first.
The first one that answers a one-word probe is used, and the answer is cached for a week in
CACHE_DIR/model_tiers.json — about five seconds at most once a week, not on every launch. Delete
that file to check again sooner. A setting that is not a tier name (a full model id) is used
exactly as given, never probed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

TIERS_FILE = Path(__file__).parent / "data" / "model_tiers.txt"
CACHE_TTL_S = 7 * 24 * 3600
PROBE_TIMEOUT_S = 30.0
PROBE_PROMPT = "Reply with the single word: OK"
#: How the CLI says an id is unavailable (first-turn result text, verified above).
MODEL_ERROR = "issue with the selected model"


def load_tiers(path: Path = TIERS_FILE) -> dict[str, list[str]]:
    tiers: dict[str, list[str]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return tiers
    for raw in lines:
        parts = raw.split("#", 1)[0].split()
        if len(parts) >= 2:
            tiers[parts[0].lower()] = parts[1:]
    return tiers


def candidates(spec: str, tiers: dict[str, list[str]] | None = None) -> list[str]:
    """A tier -> its ids, newest first. Anything else -> itself (a pinned id)."""
    tiers = load_tiers() if tiers is None else tiers
    key = str(spec or "").strip()
    return list(tiers.get(key.lower(), [key])) if key else []


def probe(model: str, cwd: Path, env: dict[str, str], timeout: float = PROBE_TIMEOUT_S) -> bool:
    """One tiny real turn: True when the CLI answered with exactly this model."""
    exe = shutil.which("claude", path=env.get("PATH"))
    if not exe:
        return False
    argv = [exe, "-p", PROBE_PROMPT, "--output-format", "stream-json", "--verbose",
            "--tools", "", "--strict-mcp-config", "--model", model]
    try:
        cwd.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    for line in done.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            return not ev.get("is_error") and model in (ev.get("modelUsage") or {})
    return False


class Resolver:
    def __init__(self, cache_path: Path, probe_fn: Callable[[str], bool],
                 tiers: dict[str, list[str]] | None = None, clock: Callable[[], float] = time.time):
        self.cache_path = Path(cache_path)
        self.probe = probe_fn
        self.tiers = load_tiers() if tiers is None else tiers
        self.clock = clock

    @classmethod
    def from_config(cls, cfg) -> "Resolver":
        from backend import config as config_mod
        from backend.brain.claude_cli import child_env
        cwd = config_mod.claude_cwd(cfg)          # the same isolated cwd as the tutor (spec §4)
        return cls(cfg.path("CACHE_DIR") / "model_tiers.json", lambda m: probe(m, cwd, child_env()))

    def resolve(self, spec: str) -> str:
        options = candidates(spec, self.tiers)
        if len(options) <= 1:
            return options[0] if options else str(spec)
        cache = self._read()
        hit = cache.get(options[-1])
        if (isinstance(hit, dict) and hit.get("model") in options
                and self.clock() - float(hit.get("at", 0)) < CACHE_TTL_S):
            return hit["model"]
        chosen = options[-1]                       # the bare alias: always accepted, never probed
        for model in options[:-1]:
            if self.probe(model):
                chosen = model
                break
        cache[options[-1]] = {"model": chosen, "at": self.clock()}
        self._write(cache)
        return chosen

    def forget(self, spec: str) -> None:
        """A remembered model stopped answering: check again at the next launch."""
        options = candidates(spec, self.tiers)
        cache = self._read()
        if options and cache.pop(options[-1], None) is not None:
            self._write(cache)

    def _read(self) -> dict:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".tiers-", suffix=".json", dir=self.cache_path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.cache_path)
        except OSError:
            pass                                  # a cache we cannot write is just a re-probe later
