"""Generate .cache/mcp.json for the claude subprocess (spec §11).

The Bunpro MCP server reads the launch snapshot; it needs the snapshot path and a path for its
READY MARKER — not the token. No credential is written here at all. Writes an empty server
list if Bunpro is not configured.

Readiness protocol (spec §4/§5b): the server touches ATAMA_MCP_READY when Claude's
`notifications/initialized` arrives (i.e. Claude is actually connected) and removes it on
shutdown. The orchestrator waits on that marker before sending the first turn and reports
`bunpro_mcp = connected` from it — a real signal, never a timer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from backend import config


def snapshot_path(cfg: config.Config) -> Path:
    return cfg.path("CACHE_DIR") / "srs" / "bunpro.json"


def ready_marker(cfg: config.Config) -> Path:
    return cfg.path("CACHE_DIR") / "srs" / "bunpro_mcp.ready"


def build(cfg: config.Config, python: str | None = None) -> dict:
    servers: dict = {}
    if cfg.BUNPRO_API_TOKEN:
        servers["bunpro"] = {
            "command": python or sys.executable,
            "args": ["-m", "backend.srs.bunpro_mcp"],
            "cwd": str(config.REPO_ROOT),
            "env": {
                "ATAMA_SNAPSHOT": str(snapshot_path(cfg)),
                "ATAMA_MCP_READY": str(ready_marker(cfg)),
                "PYTHONPATH": str(config.REPO_ROOT),
            },
        }
    return {"mcpServers": servers}


def write(cfg: config.Config | None = None) -> Path:
    cfg = cfg or config.load()
    out = cfg.path("CACHE_DIR") / "mcp.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build(cfg), indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    p = write()
    d = json.loads(p.read_text(encoding="utf-8"))
    print(f"mcp-config: wrote {p.relative_to(config.REPO_ROOT).as_posix()} ({len(d['mcpServers'])} server(s), no credentials)")
