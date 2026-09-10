"""Stop everything, in one command.

    python -m backend.tools.down        # or: make stop

The tutor itself exits with Ctrl+C and cleans up after itself — the claude subprocess, the speech
queue, the WebSocket server. What survives that is the containers, because they are started
detached and configured `restart: unless-stopped`, so they come back after a reboot until
something says otherwise. This is that something.
"""
from __future__ import annotations

import sys

from backend.tools.up import compose


def main(argv: list[str]) -> int:
    keep_data = "--volumes" not in argv
    code = compose("down") if keep_data else compose("down", "--volumes")
    if code == 127:
        return 0                     # no docker: nothing of ours is running under it either
    print("containers stopped." if code == 0 else "docker compose down reported a problem.")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
