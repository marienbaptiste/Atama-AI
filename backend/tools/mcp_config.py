"""Generate .cache/mcp.json for the claude subprocess (spec §11).

Two servers, neither of which is given a credential:
  bunpro  — reads the launch snapshot (ADR-024); needs the snapshot path, not the token.
  search  — talks to the user's own SearxNG (ADR-028); SearxNG needs no key at all.

Each also gets the path where it should write its readiness marker, which the brain waits on
before sending the first turn (spec §4).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from backend import config

BUNPRO_TOOLS = tuple(f"mcp__bunpro__{t}" for t in ("get_review_queue", "get_ghost_reviews", "get_grammar_progress"))
SEARCH_TOOLS = ("mcp__search__search",)


def snapshot_path(cfg: config.Config) -> Path:
    return cfg.path("CACHE_DIR") / "srs" / "bunpro.json"


def ready_marker(cfg: config.Config, server: str = "bunpro") -> Path:
    return cfg.path("CACHE_DIR") / "srs" / f"{server}_mcp.ready"


def _python() -> str:
    return sys.executable


def build(cfg: config.Config) -> dict:
    servers: dict = {}
    common = {"cwd": str(config.REPO_ROOT), "command": _python()}
    if cfg.BUNPRO_API_TOKEN:
        servers["bunpro"] = {
            **common,
            "args": ["-m", "backend.srs.bunpro_mcp"],
            "env": {
                "ATAMA_SNAPSHOT": str(snapshot_path(cfg)),
                "ATAMA_MCP_READY": str(ready_marker(cfg, "bunpro")),
                "PYTHONPATH": str(config.REPO_ROOT),
            },
        }
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
    out: dict[str, Path] = {}
    if cfg.BUNPRO_API_TOKEN:
        out["bunpro_mcp"] = ready_marker(cfg, "bunpro")
    if cfg.SEARXNG_URL:
        out["search"] = ready_marker(cfg, "search")
    return out


def allowed_tools(cfg: config.Config) -> tuple[str, ...]:
    """The exact tool names the tutor may call — belt-and-braces beside --strict-mcp-config."""
    tools: tuple[str, ...] = ()
    if cfg.BUNPRO_API_TOKEN:
        tools += BUNPRO_TOOLS
    if cfg.SEARXNG_URL:
        tools += SEARCH_TOOLS
    return tools


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
