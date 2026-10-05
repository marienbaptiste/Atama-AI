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

**Finding a model nobody has listed yet (user, 2026-10-06).** The list above is the floor, not the
ceiling: on 2026-10-06 the app was still running Sonnet 5 three weeks after Sonnet 5.5 shipped,
because a release needs a line in a file and nobody added it. There is no way to ASK for the
lineup here (the Models API needs an API key, which this app refuses by design, ADR-001, and the
CLI cannot list models), so `discover()` guesses forward from the newest id it knows and lets the
CLI judge: `claude-sonnet-5-5` -> try `claude-sonnet-6`, then `claude-sonnet-5-6`. A refused id
costs 1.8-2.9 s (measured 2026-10-06) and is cached for a week like a successful one, and the
guessing runs OFF the launch path, so a launch never waits for it. It cannot invent a new family
name (nothing would have guessed "Fable"); for that, the file is still the answer.

**The CLI can be too old for a model that exists.** Verified 2026-10-06: Opus 5.5 on CLI 2.1.159
answers "does not support this model; version 2.1.280 or newer is required". That is not "no such
model", and silently dropping a generation is the wrong answer to it, so the refusal is kept
(`Resolver.cli_too_old`), persisted in the cache, printed in red at launch and reported by
`make doctor` with the command that fixes it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from backend import constants

TIERS_FILE = Path(__file__).parent / "data" / "model_tiers.txt"
CACHE_TTL_S = 7 * 24 * 3600
PROBE_TIMEOUT_S = 30.0
PROBE_PROMPT = "Reply with the single word: OK"
#: How the CLI says an id is unavailable (first-turn result text, verified above).
MODEL_ERROR = "issue with the selected model"
#: How it says the id exists but this CLI is too old, with its own required version.
TOO_OLD = constants.CLAUDE_CLI_TOO_OLD_FOR_MODEL
_REQUIRED_VERSION = re.compile(r"version\s+(\d+\.\d+\.\d+)\s+or newer")
#: An id this file knows: claude-<line>-<major>[-<minor>][-<date>].
_MODEL_ID = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d+))?(?:-(\d{8}))?$")


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


@dataclass(frozen=True)
class Probe:
    """What one probe learned. Truthy when the CLI answered with that exact model, so a caller
    that only cares whether it worked can keep treating this as a bool."""

    ok: bool
    message: str = ""

    def __bool__(self) -> bool:
        return self.ok

    @property
    def needs_newer_cli(self) -> str:
        """The CLI version this model needs, when that is why it was refused; "" otherwise."""
        if TOO_OLD not in self.message:
            return ""
        found = _REQUIRED_VERSION.search(self.message)
        return found.group(1) if found else "a newer version"


def forward_candidates(newest: str) -> list[str]:
    """Ids to try for something newer than `newest`, newest guess first.

    `claude-sonnet-5-5` -> [`claude-sonnet-6`, `claude-sonnet-5-6`];
    `claude-haiku-4-5-20251001` -> [`claude-haiku-5`, `claude-haiku-4-6`] (the dateless form, which
    is what the ids have used since the 4.6 generation). Anything that is not a model id of that
    shape yields nothing: this guesses within a naming scheme, it does not invent one.
    """
    m = _MODEL_ID.match(str(newest or "").strip())
    if not m:
        return []
    line, major, minor = m.group(1), int(m.group(2)), m.group(3)
    out = [f"claude-{line}-{major + 1}"]
    if minor is not None:
        out.append(f"claude-{line}-{major}-{int(minor) + 1}")
    return out


def probe(model: str, cwd: Path, env: dict[str, str], timeout: float = PROBE_TIMEOUT_S) -> Probe:
    """One tiny real turn: ok when the CLI answered with exactly this model."""
    exe = shutil.which("claude", path=env.get("PATH"))
    if not exe:
        return Probe(False, "no claude on PATH")
    argv = [exe, "-p", PROBE_PROMPT, "--output-format", "stream-json", "--verbose",
            "--tools", "", "--strict-mcp-config", "--model", model]
    try:
        cwd.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return Probe(False, f"{type(exc).__name__}: {exc}")
    for line in done.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            ok = not ev.get("is_error") and model in (ev.get("modelUsage") or {})
            return Probe(ok, "" if ok else str(ev.get("result") or "")[:300])
    return Probe(False, (done.stderr or "").strip()[:300])


class Resolver:
    def __init__(self, cache_path: Path, probe_fn: Callable[[str], bool],
                 tiers: dict[str, list[str]] | None = None, clock: Callable[[], float] = time.time):
        self.cache_path = Path(cache_path)
        self.probe = probe_fn
        self.tiers = load_tiers() if tiers is None else tiers
        self.clock = clock
        #: (model, required version) when a probe was refused for being newer than this CLI.
        self.cli_too_old: tuple[str, str] | None = None

    @classmethod
    def from_config(cls, cfg) -> "Resolver":
        from backend import config as config_mod
        from backend.brain.claude_cli import child_env
        cwd = config_mod.claude_cwd(cfg)          # the same isolated cwd as the tutor (spec §4)
        return cls(cfg.path("CACHE_DIR") / "model_tiers.json", lambda m: probe(m, cwd, child_env()))

    def options_for(self, spec: str) -> list[str]:
        """The ids to try for `spec`, newest first: anything `discover()` found, then the file's."""
        options = candidates(spec, self.tiers)
        if len(options) <= 1:
            return options
        found = (self._read().get("discovered") or {}).get(options[-1])
        if isinstance(found, dict) and (model := str(found.get("model") or "")) and model not in options:
            if self.clock() - float(found.get("at", 0)) < CACHE_TTL_S * 52:   # a found id does not go stale
                return [model, *options]
        return options

    def resolve(self, spec: str) -> str:
        options = self.options_for(spec)
        if len(options) <= 1:
            return options[0] if options else str(spec)
        cache = self._read()
        hit = cache.get(options[-1])
        if (isinstance(hit, dict) and hit.get("model") in options
                and self.clock() - float(hit.get("at", 0)) < CACHE_TTL_S):
            return hit["model"]
        chosen = options[-1]                       # the bare alias: always accepted, never probed
        for model in options[:-1]:
            result = self.probe(model)
            if result:
                chosen = model
                break
            self._note_refusal(model, result)
        cache = self._read()                       # re-read: _note_refusal may have written
        cache[options[-1]] = {"model": chosen, "at": self.clock()}
        self._write(cache)
        return chosen

    def _note_refusal(self, model: str, result) -> None:
        """Keep a refusal that means "your CLI is too old", so the launch and the doctor can say so
        instead of quietly using an older model (user, 2026-10-06)."""
        required = getattr(result, "needs_newer_cli", "")
        if not required:
            return
        self.cli_too_old = (model, required)
        cache = self._read()
        cache["cli_too_old"] = {"model": model, "required": required, "at": self.clock()}
        self._write(cache)

    def discover(self, spec: str) -> str | None:
        """Guess forward from the newest id known for `spec` and return one the CLI accepts.

        Off the launch path by design: a refused guess costs a couple of seconds, and there is
        usually nothing to find. Checked once a week, like a resolution. Returns the new id, or
        None when nothing newer answers.
        """
        options = self.options_for(spec)
        if len(options) <= 1:
            return None
        alias = options[-1]
        cache = self._read()
        looked = (cache.get("looked") or {}).get(alias)
        if isinstance(looked, dict) and self.clock() - float(looked.get("at", 0)) < CACHE_TTL_S:
            return None
        found = None
        for guess in forward_candidates(options[0]):
            result = self.probe(guess)
            if result:
                found = guess
                break
            self._note_refusal(guess, result)
        cache = self._read()
        cache.setdefault("looked", {})[alias] = {"at": self.clock()}
        if found is not None:
            cache.setdefault("discovered", {})[alias] = {"model": found, "at": self.clock()}
            cache.pop(alias, None)                 # the remembered resolution is a generation old
        self._write(cache)
        return found

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
