"""Fetch the default avatar so a fresh clone has a face.

    python -m backend.tools.get_avatar [persona]

Downloads TalkingHead's reference avatar from ITS OWN repository rather than vendoring a copy
here. That is deliberate: the file is CC BY-NC 4.0, and pulling it from source at setup time
keeps this repository's own tree cleanly MIT-licensed while still giving a working default.

**It is non-commercial.** Replace it before shipping anything commercial — and see LICENSE §1
first, because making your own at Ready Player Me does not lift the restriction.
"""
from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

from backend import config, prompt

SOURCE = "https://raw.githubusercontent.com/met4citizen/TalkingHead/main/avatars/brunette.glb"
LICENCE = "CC BY-NC 4.0 (Ready Player Me, via the TalkingHead project) — NON-COMMERCIAL"


def main(argv: list[str]) -> int:
    persona = argv[1] if len(argv) > 1 else config.load().TUTOR_PERSONA
    declared = prompt.declared_avatar(persona)
    if not declared:
        print(f"persona {persona!r} declares no avatar "
              f"(add `<!-- avatar: name.glb -->` to prompts/{persona}.md)", file=sys.stderr)
        return 2
    target = config.REPO_ROOT / "frontend" / "public" / declared
    if target.exists():
        print(f"{target.relative_to(config.REPO_ROOT)} already exists — leaving it alone.")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"fetching the default avatar for {persona} -> {declared}")
    try:
        with urllib.request.urlopen(SOURCE, timeout=120) as response:
            target.write_bytes(response.read())
    except (urllib.error.URLError, OSError) as exc:
        print(f"could not fetch {SOURCE}: {exc}", file=sys.stderr)
        return 1
    print(f"  {target.stat().st_size / 1e6:.1f} MB")
    print(f"  licence: {LICENCE}")
    print(f"  replace it before any commercial use — see LICENSE section 1\n")
    from backend.tools import check_avatar
    return check_avatar.check(target, persona)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except config.ConfigError as exc:      # a malformed settings.json: one line, not a traceback
        sys.exit(str(exc))
