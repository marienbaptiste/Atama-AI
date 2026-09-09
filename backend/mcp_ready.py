"""MCP readiness marker, shared by our stdio MCP servers (spec §4).

Claude Code prints `init` with `mcp_servers: pending` and never announces the connection on its
stdout (verified 2026-09-09). A user turn sent before a server connects reaches a model with no
tools, silently. So each server announces itself: on the client's `notifications/initialized` it
writes a marker file, and the orchestrator waits on that signal — never on a sleep.
"""
from __future__ import annotations

import atexit
import datetime as dt
import json
import os
import sys
from pathlib import Path

import mcp.types as types


def marker_path(env_var: str, override: Path | None = None) -> Path | None:
    if override is not None:
        return override
    raw = os.environ.get(env_var, "").strip()
    return Path(raw) if raw else None


def announce_ready(path: Path | None) -> Path | None:
    """Write the marker. Called from the `notifications/initialized` handler."""
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": os.getpid(), "connected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}),
        encoding="utf-8",
    )
    return path


def clear_marker(path: Path | None) -> None:
    """Remove the marker only if it is OURS.

    The path is shared by every instance of a server. A stale one exiting must not delete the
    marker a freshly started instance just wrote — that race made a connected server look like a
    readiness timeout (found 2026-09-09).
    """
    if path is None:
        return
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") != os.getpid():
            return
    except (OSError, ValueError):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def install(server, path_getter, label: str) -> None:
    """Wire readiness into an MCPServer: announce on initialize, clean up on exit.

    `path_getter` is a callable so tests can point the marker somewhere else after import.
    """

    async def _on_initialized(ctx, params) -> None:
        announce_ready(path_getter())
        print(f"{label}: client initialized -> ready marker written", file=sys.stderr)

    server._lowlevel_server.add_notification_handler(
        "notifications/initialized", types.NotificationParams, _on_initialized
    )
    atexit.register(lambda: clear_marker(path_getter()))
