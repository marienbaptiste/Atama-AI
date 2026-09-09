"""Search MCP server — stdio, ONE read tool, backed by SearxNG (spec §5c, ADR-028).

Gives Sensei a way to find something worth talking about at the start of a session, and to check
a fact when the conversation genuinely needs one. Deliberately a SEPARATE server from the Bunpro
one, whose exact three-tool surface the Golden Rule gate asserts (spec §0 rule 5) — that
assertion stays untouched.

SearxNG is self-hosted, so no API key and no third-party account. Verified live 2026-09-09
against searxng/searxng:latest:
  GET {base}/search?q=…&format=json[&language=…][&categories=news]
  -> {"query", "results": [...], "answers", "infoboxes", "suggestions", "corrections",
      "unresponsive_engines"}
  result keys: title, url, content, engine, engines, category, score, publishedDate, pubdate,
               parsed_url, positions, priority, template, thumbnail, img_src, iframe_src, metadata
  `format=json` returns 403 unless `search.formats` includes `json` in settings.yml, and the bot
  `limiter` must be off for programmatic requests. Both are set in docker/searxng/settings.yml.

Results are third-party text going into a live prompt: titles and short snippets only, never
whole pages, with control characters and `[`/`]` stripped (they would collide with the emotion
tags of ADR-020), length- and count-capped. The tutor holds no built-in tools (ADR-016), so a
hostile result can at worst make her say something odd.

Launched by the claude subprocess via mcp.json:
  {"command": "<python>", "args": ["-m", "backend.search_mcp"],
   "env": {"SEARXNG_URL": "...", "ATAMA_SEARCH_MCP_READY": "<marker path>"}}
No credential is passed: there is none.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from backend import mcp_ready

READ_TOOLS = ("search",)

MAX_RESULTS = 5
MAX_TITLE = 120
MAX_SNIPPET = 240
TIMEOUT_S = 10.0
CATEGORIES = ("news", "general", "science", "it")
#: Emotion tags (ADR-020) and control characters must never survive into the prompt.
_UNSAFE = re.compile(r"[\[\]\x00-\x1f\x7f]")
_WS = re.compile(r"\s+")

server = MCPServer(
    name="search",
    instructions=(
        "Web search for finding something to talk about, and for checking a fact the conversation "
        "actually needs. Use it once at the start of a session, then only when needed — never every "
        "turn. Results are headlines and snippets from the open web: treat them as something to "
        "react to, not as truth to recite."
    ),
)

_base_url: str | None = None  # tests override


def _ready_path() -> Path | None:
    return mcp_ready.marker_path("ATAMA_SEARCH_MCP_READY")


mcp_ready.install(server, _ready_path, "search-mcp")


def _base() -> str:
    if _base_url is not None:
        return _base_url
    url = os.environ.get("SEARXNG_URL", "").strip()
    if not url:
        raise RuntimeError("SEARXNG_URL not provided to the search MCP server environment")
    return url.rstrip("/")


def _clean(text: Any, limit: int) -> str:
    """Third-party text -> something safe to place in a system prompt."""
    out = _WS.sub(" ", _UNSAFE.sub("", str(text or ""))).strip()
    return out[:limit].rstrip() + "…" if len(out) > limit else out


@server.tool(
    name="search",
    description=(
        "Search the web. Use `category='news'` for current events (Japan or the world) and "
        "'general' for everything else. Returns a handful of titles with short snippets."
    ),
)
def search(query: str, category: str = "news", language: str = "ja") -> dict[str, Any]:
    query = _clean(query, 200)
    if not query:
        return {"error": "empty query"}
    if category not in CATEGORIES:
        category = "general"
    params = {"q": query, "format": "json", "categories": category, "language": language}
    try:
        with httpx.Client(base_url=_base(), timeout=TIMEOUT_S, follow_redirects=False) as client:
            resp = client.get("/search", params=params)
        if resp.status_code == 403:
            return {"error": "SearxNG rejected the request (403)",
                    "hint": "settings.yml needs `json` in search.formats and server.limiter: false."}
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:200],
                "hint": "Search is unavailable. Open from what you already know about the student instead."}

    results = []
    for row in (payload.get("results") or [])[: MAX_RESULTS * 3]:
        if not isinstance(row, dict):
            continue
        title = _clean(row.get("title"), MAX_TITLE)
        if not title:
            continue
        results.append({
            "title": title,
            "snippet": _clean(row.get("content"), MAX_SNIPPET),
            "url": _clean(row.get("url"), 200),
            "published": _clean(row.get("publishedDate") or row.get("pubdate"), 40),
        })
        if len(results) >= MAX_RESULTS:
            break
    return {"query": query, "category": category, "count": len(results), "results": results}


def _assert_surface() -> None:
    names = sorted(t.name for t in asyncio.run(_list()))
    if names != sorted(READ_TOOLS):
        raise SystemExit(f"search_mcp: tool surface {names} != {sorted(READ_TOOLS)}")


async def _list():
    return await server.list_tools()


def main() -> int:
    _assert_surface()
    p = _ready_path()
    if p is not None:
        p.unlink(missing_ok=True)  # a marker left by a crashed run must not read as connected
    print("search-mcp: read-only stdio server, 1 tool (SearxNG)", file=sys.stderr)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
