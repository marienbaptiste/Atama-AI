"""Bunpro MCP server — stdio, READ TOOLS ONLY, reads the launch snapshot (spec §0/§5, ADR-021/023).

Fetch policy (user directive 2026-09-09): Bunpro is contacted ONLY at app launch and on manual
refresh, by the orchestrator. This server never makes a network request: its three tools read
the snapshot the orchestrator stored at `<CACHE_DIR>/srs/bunpro.json` and report how old it is.
Consequently it needs NO token — nothing secret enters this process.

Exactly three tools. The read-only gate asserts the surface statically; `_assert_surface()`
asserts it again at startup.

Verified against mcp 2.2.0 (2026-09-09): `from mcp.server.mcpserver import MCPServer`;
`MCPServer(name=..., instructions=...)`, `@server.tool(name=..., description=...)`,
`server.run("stdio")`, `await server.call_tool(name, arguments)` for in-process tests.
(mcp 1.x's `mcp.server.fastmcp.FastMCP` no longer exists.)

Launched by the claude subprocess via mcp.json:
  {"command": "<python>", "args": ["-m", "backend.srs.bunpro_mcp"],
   "env": {"ATAMA_SNAPSHOT": "<path>", "ATAMA_MCP_READY": "<marker path>"}}

Readiness protocol: Claude Code emits no "MCP connected" event on its stream-json stdout
(verified 2026-09-09), so this server announces it. When the client's `notifications/initialized`
arrives — the SDK runs that handler "after the runner has marked the connection initialized" —
we touch ATAMA_MCP_READY with `{pid, connected_at}`; it is removed at shutdown. The orchestrator
waits on that marker before the first turn and derives `bunpro_mcp = connected` from it.
"""
from __future__ import annotations

import asyncio
import atexit
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server.mcpserver import MCPServer

from backend.srs import bunpro as bp
from backend.srs.profile import snapshot_load

READ_TOOLS = ("get_review_queue", "get_ghost_reviews", "get_grammar_progress")

server = MCPServer(
    name="bunpro",
    instructions=(
        "Read-only view of the student's Bunpro grammar SRS as of the last sync (app launch or the "
        "student's manual refresh). Data is a snapshot, not live; each answer says how old it is. "
        "Use sparingly: when the student asks what to practise, or at most every ~15 minutes."
    ),
)

_snapshot_path: Path | None = None  # tests override; production reads ATAMA_SNAPSHOT
_ready_path: Path | None = None     # tests override; production reads ATAMA_MCP_READY


def _marker() -> Path | None:
    if _ready_path is not None:
        return _ready_path
    p = os.environ.get("ATAMA_MCP_READY", "").strip()
    return Path(p) if p else None


def announce_ready() -> Path | None:
    """Write the ready marker. Called from the `notifications/initialized` handler."""
    m = _marker()
    if m is None:
        return None
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(json.dumps({"pid": os.getpid(), "connected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}),
                 encoding="utf-8")
    return m


def clear_marker() -> None:
    m = _marker()
    if m is not None:
        try:
            m.unlink()
        except FileNotFoundError:
            pass


async def _on_initialized(ctx, params) -> None:  # signature: (ServerRequestContext, NotificationParams)
    announce_ready()
    print("bunpro-mcp: client initialized -> ready marker written", file=sys.stderr)


server._lowlevel_server.add_notification_handler("notifications/initialized", types.NotificationParams, _on_initialized)
atexit.register(clear_marker)


def _path() -> Path:
    if _snapshot_path is not None:
        return _snapshot_path
    p = os.environ.get("ATAMA_SNAPSHOT", "").strip()
    if not p:
        raise RuntimeError("ATAMA_SNAPSHOT not provided to the MCP server environment")
    return Path(p)


def _load() -> tuple[dict[str, Any], dict[str, Any]]:
    """-> (raw snapshot, meta{synced_at, age_minutes}). Raises RuntimeError if there is no snapshot."""
    raw, fetched_at = snapshot_load(_path())
    if raw is None:
        raise RuntimeError("no Bunpro snapshot yet — the student should press Refresh in the app")
    age = None
    if fetched_at:
        try:
            age = round((dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(fetched_at)).total_seconds() / 60)
        except ValueError:
            age = None
    return raw, {"synced_at": fetched_at or "unknown", "age_minutes": age}


def _err(e: Exception) -> dict[str, Any]:
    return {"error": f"{type(e).__name__}: {e}"[:300],
            "hint": "Tell the student Bunpro data is unavailable right now; they can press Refresh in the app."}


@server.tool(name="get_review_queue", description="Reviews due on Bunpro (grammar and vocab counts) and how many grammar reviews come tomorrow — as of the last sync.")
def get_review_queue() -> dict[str, Any]:
    try:
        raw, meta = _load()
        p = bp.parse(raw)
        return {**meta, "due_grammar": p.due_grammar, "due_vocab": p.due_vocab,
                "grammar_tomorrow": p.forecast_tomorrow_grammar, "grammar_later": p.forecast_later_grammar}
    except (RuntimeError, ValueError, TypeError, OSError) as e:
        return _err(e)


@server.tool(name="get_ghost_reviews", description="The student's Bunpro ghost reviews — grammar points they keep getting wrong. Best material to weave into conversation. As of the last sync.")
def get_ghost_reviews() -> dict[str, Any]:
    try:
        raw, meta = _load()
        p = bp.parse(raw)
        return {**meta, "count": len(p.ghosts),
                "ghosts": [{"grammar": g.title, "meaning": g.meaning, "jlpt": g.jlpt, "streak": g.streak} for g in p.ghosts]}
    except (RuntimeError, ValueError, TypeError, OSError) as e:
        return _err(e)


@server.tool(name="get_grammar_progress", description="Bunpro JLPT grammar progress per level (learned/total) and the grammar points still at beginner SRS stage. As of the last sync.")
def get_grammar_progress() -> dict[str, Any]:
    try:
        raw, meta = _load()
        p = bp.parse(raw)
        return {**meta, "studying": p.current_jlpt(), "jlpt": p.jlpt_grammar,
                "beginner_stage": [{"grammar": g.title, "meaning": g.meaning, "jlpt": g.jlpt} for g in p.weak_grammar]}
    except (RuntimeError, ValueError, TypeError, OSError) as e:
        return _err(e)


def _assert_surface() -> None:
    async def _names():
        return sorted(t.name for t in await server.list_tools())
    names = asyncio.run(_names())
    if names != sorted(READ_TOOLS):
        raise SystemExit(f"bunpro_mcp: tool surface {names} != {sorted(READ_TOOLS)} — Golden Rule (spec §0)")


def main() -> int:
    _assert_surface()
    clear_marker()  # a stale marker from a crashed run must not look like "connected"
    print("bunpro-mcp: read-only stdio server, 3 tools, snapshot reader (no network)", file=sys.stderr)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
