"""Minimal settings CLI until the settings page exists (M5).

  python -m backend.tools.settings_cli set BUNPRO_API_TOKEN     # prompts, hidden input
  python -m backend.tools.settings_cli set CLAUDE_MODEL sonnet  # non-secret, inline
  python -m backend.tools.settings_cli show                     # secrets shown as {set, hint}

Secrets are read with getpass so they never appear in shell history or terminal scrollback.
"""
from __future__ import annotations

import getpass
import json
import sys

from backend import config


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("set", "show"):
        print(__doc__)
        return 2
    if argv[0] == "show":
        print(json.dumps(config.load().public_view(), indent=2, ensure_ascii=False))
        return 0
    if len(argv) < 2:
        print("usage: set KEY [VALUE]", file=sys.stderr)
        return 2
    key = argv[1].upper()
    if key not in config.KEYS:
        # Never echo the argument back: a common mistake is pasting the TOKEN where the KEY goes,
        # and echoing it would put the secret in terminal scrollback a second time.
        print("unknown setting name (not echoed). Usage: set KEY  — e.g. `set BUNPRO_API_TOKEN`, "
              "then paste the token at the hidden prompt.", file=sys.stderr)
        print("If you just pasted a token here by mistake: it is in your shell history — regenerate it "
              "and run Clear-History / delete (Get-PSReadLineOption).HistorySavePath.", file=sys.stderr)
        print(f"Known keys: {', '.join(config.KEYS)}", file=sys.stderr)
        return 2
    if key in config.SECRET_KEYS:
        if len(argv) > 2:
            print("refusing to take a secret on the command line (shell history). Run without VALUE.", file=sys.stderr)
            return 2
        value = getpass.getpass(f"{key} (input hidden): ").strip()
    else:
        value = argv[2] if len(argv) > 2 else input(f"{key}: ").strip()
    path = config.save({key: value})
    shown = config.hint(value) if key in config.SECRET_KEYS else value
    print(f"saved {key} = {shown} -> {path.relative_to(config.REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
