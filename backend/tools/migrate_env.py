"""Move settings out of `.env` into `settings.json`, where the app now reads them.

    python -m backend.tools.migrate_env            # do it (backs .env up first)
    python -m backend.tools.migrate_env --dry-run  # say what would move, change nothing

**The app no longer reads `.env` at all** (user, 2026-09-12; ADR-022 amendment): the settings page
is the interface, and a second file silently overriding it was a trap. So this moves everything
the schema knows, **including the API tokens** — left behind they would simply stop working.
`settings.json` is git-ignored and written 0600, which is where a credential belongs anyway.
What stays in `.env`:

* SETTINGS_FILE — it says where settings.json IS, so it cannot live inside it. Set
  `ATAMA_SETTINGS_FILE` in the environment instead if you need it somewhere else;
* anything config.py does not read. SEARXNG_SECRET used to be the one reason to keep a `.env`
  at all; the launcher generates it into the state directory now, so you can delete the file;
* every comment.

Keys with an empty value are dropped: an empty value overrides nothing (`config.load` skips it).
The effective configuration must be identical before and after; if it is not, both files are put
back and the tool fails. Values are never printed — only key names.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from backend import config

#: Bootstrap keys: needed to FIND settings.json, so they cannot move into it.
KEEP = frozenset({"SETTINGS_FILE"})
_ASSIGN = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def plan(env_path: Path) -> tuple[dict[str, str], list[str], list[str]]:
    """(to_move {key: raw value}, to_drop [keys with empty values], invalid [unparseable])."""
    by_key = {s.key: s for s in config.SCHEMA}
    move: dict[str, str] = {}
    drop: list[str] = []
    invalid: list[str] = []
    for key, raw in config.read_dotenv(env_path).items():
        s = by_key.get(key)
        if s is None or key in KEEP:
            continue
        if raw == "":
            drop.append(key)
            continue
        try:
            config._coerce(s, raw)
        except ValueError:
            invalid.append(key)
            continue
        move[key] = raw
    return move, drop, invalid


def _effective(settings_path: Path, env_path: Path | None) -> dict[str, Any]:
    """What the app sees. With `env_path`, the OLD resolution that still read `.env` — which is
    what the student had configured, and what the migration has to reproduce without it."""
    env = config.env_overrides() if env_path is None else {**config.read_dotenv(env_path),
                                                           **config.env_overrides()}
    return dict(config.load(settings_path, env=env)._values)


def migrate(env_path: Path, settings_path: Path, dry_run: bool = False) -> dict[str, Any]:
    move, drop, invalid = plan(env_path)
    report: dict[str, Any] = {"moved": sorted(move), "dropped": sorted(drop),
                              "invalid": sorted(invalid), "backup": None}
    if dry_run or not (move or drop):
        return report

    before = _effective(settings_path, env_path)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = env_path.with_name(f".env.bak-{stamp}")        # git-ignored by `.env.*`
    shutil.copy2(env_path, backup)
    settings_before = settings_path.read_bytes() if settings_path.exists() else None
    report["backup"] = str(backup)
    try:
        if move:
            config.save(move, settings_path)
        gone = set(move) | set(drop)
        raw = env_path.read_text(encoding="utf-8")
        nl = "\r\n" if "\r\n" in raw else "\n"
        kept = [line for line in raw.splitlines()
                if not ((m := _ASSIGN.match(line)) and m.group(1) in gone)]
        note = (f"# {len(gone)} settings moved to settings.json on {stamp} by "
                f"backend.tools.migrate_env (backup: {backup.name}).{nl}"
                f"# atama-AI does NOT read this file any more - edit everything in the settings"
                f" panel.{nl}# Nothing left here is read by anything - delete the file.{nl}")
        fd, tmp = tempfile.mkstemp(prefix=".env-", dir=env_path.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(note + nl.join(kept) + nl)
        os.replace(tmp, env_path)
        after = _effective(settings_path, None)          # the new world: no .env at all
        if after != before:
            changed = sorted(k for k in before
                             if before[k] != after.get(k) and k.upper() not in KEEP)
            raise RuntimeError(f"the effective configuration changed for {changed}; restored")
    except BaseException:
        shutil.copy2(backup, env_path)
        if settings_before is None:
            settings_path.unlink(missing_ok=True)
        else:
            settings_path.write_bytes(settings_before)
        raise
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report only; change nothing")
    args = parser.parse_args(argv)
    env_path = config.REPO_ROOT / ".env"
    if not env_path.is_file():
        print("no .env - nothing to migrate")
        return 0
    report = migrate(env_path, config.load().settings_path, dry_run=args.dry_run)
    would = args.dry_run
    print(f"{'would move' if would else 'moved'} {len(report['moved'])} setting(s) to settings.json: "
          f"{', '.join(report['moved']) or '-'}")
    if report["dropped"]:
        print(f"{'would drop' if would else 'dropped'} {len(report['dropped'])} empty key(s) "
              f"(they override nothing): {', '.join(report['dropped'])}")
    if report["invalid"]:
        print(f"left in .env because the value does not parse: {', '.join(report['invalid'])}")
    print("left in .env: SETTINGS_FILE and keys nothing reads any more (SEARXNG_SECRET is"
          " generated by the launcher now). The file can be deleted.")
    if report["backup"]:
        print(f"backup: {report['backup']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except config.ConfigError as exc:      # a malformed settings.json: one line, not a traceback
        sys.exit(str(exc))
