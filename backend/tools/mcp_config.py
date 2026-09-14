"""Generate .cache/mcp.json for the claude subprocess (spec §11).

One server, given no credential:
  search  — talks to the user's own SearxNG (ADR-028); SearxNG needs no key at all.

There is no SRS server. A Bunpro one read the launch snapshot until 2026-09-14, when it was
retired (ADR-039): the Student Profile already carried everything it could say, and the tutor
never called it. WaniKani and Bunpro reach her through the profile only.

The server also gets the path where it should write its readiness marker, which the brain waits
on before sending the first turn (spec §4).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from backend import config

SEARCH_TOOLS = ("mcp__search__search",)


def ready_marker(cfg: config.Config, server: str = "search") -> Path:
    return cfg.path("CACHE_DIR") / "srs" / f"{server}_mcp.ready"


def _python() -> str:
    return sys.executable


def build(cfg: config.Config) -> dict:
    servers: dict = {}
    common = {"cwd": str(config.REPO_ROOT), "command": _python()}
    if cfg.SEARXNG_URL:
        servers["search"] = {
            **common,
            "args": ["-m", "backend.search_mcp"],
            "env": {
                "SEARXNG_URL": cfg.SEARXNG_URL,
                "ATAMA_SEARCH_MCP_READY": str(ready_marker(cfg, "search")),
                "PYTHONPATH": str(config.REPO_ROOT),
            },
        }
    return {"mcpServers": servers}


def markers(cfg: config.Config) -> dict[str, Path]:
    """Status-chip name -> readiness marker, for the servers actually configured."""
    return {"search": ready_marker(cfg, "search")} if cfg.SEARXNG_URL else {}


def allowed_tools(cfg: config.Config) -> tuple[str, ...]:
    """The exact tool names the tutor may call — belt-and-braces beside --strict-mcp-config."""
    return SEARCH_TOOLS if cfg.SEARXNG_URL else ()


def write(cfg: config.Config | None = None) -> Path:
    cfg = cfg or config.load()
    out = cfg.path("CACHE_DIR") / "mcp.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build(cfg), indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    p = write()
    d = json.loads(p.read_text(encoding="utf-8"))
    print(f"mcp-config: wrote {p.relative_to(config.REPO_ROOT).as_posix()} "
          f"({', '.join(d['mcpServers']) or 'no servers'}; no credentials)")
