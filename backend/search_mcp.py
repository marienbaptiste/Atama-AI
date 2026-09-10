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
import concurrent.futures
import datetime as dt
import email.utils
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
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

#: Headline feeds merged into `news` results (spec §5c). Data, not code: see the file's header.
FEEDS_FILE = Path(__file__).parent / "data" / "news_feeds.txt"
#: A headline older than this is not news. NHK's www3 RSS froze on 2026-08-09 and still answers
#: 200 with month-old items; without a cutoff she would present them as today's news. An undated
#: item is dropped for the same reason — it cannot be shown as current.
MAX_AGE_HOURS = 72
#: Feeds are fetched concurrently and a search waits at most FEEDS_DEADLINE_S for them.
#: Measured 2026-09-10: fetched one after another with a 6 s timeout each, stalled feeds made a
#: single search take 58-79 s — on the opening turn. The same day Yahoo took 2.5 s to connect and
#: 5-12 s to first byte (curl agrees), so a short per-request timeout would mean Yahoo never
#: contributes. Hence two numbers: the answer waits FEEDS_DEADLINE_S; the fetch itself keeps going
#: up to FEED_TIMEOUT_S in the background and lands in the cache for the next search.
FEED_TIMEOUT_S = 20.0
FEEDS_DEADLINE_S = 4.0
#: A session searches rarely, and the headlines barely change in ten minutes. Caching per feed
#: makes a second search instant and is the polite thing to do to Yahoo.
FEED_CACHE_S = 600
_cache: dict[str, tuple[float, list]] = {}
#: One fetch per feed at a time, however many searches ask for it while it is in flight.
_inflight: dict[str, concurrent.futures.Future] = {}
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="feed")
#: Items kept per provider after ranking, before interleaving.
PER_PROVIDER = 4
_UA = "atama-ai/0.0 (personal Japanese study tool)"

_feeds: tuple[tuple[str, str], ...] | None = None  # tests override; None = read FEEDS_FILE
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


def load_feeds(path: Path = FEEDS_FILE) -> tuple[tuple[str, str], ...]:
    """`provider/label  https-url` per line -> ((name, url), ...). Comments and blanks skipped;
    a non-https URL is rejected rather than trusted."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    out = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("https://"):
            out.append((parts[0], parts[1]))
    return tuple(out)


def _active_feeds() -> tuple[tuple[str, str], ...]:
    return load_feeds() if _feeds is None else _feeds


def _parse_feed(content: bytes) -> list[tuple[str, str, str, str, dt.datetime]]:
    """RSS bytes -> [(title, description, link, pubDate, when)]. Undated items are dropped here:
    a headline with no date cannot be offered as current news."""
    out = []
    for node in ET.fromstring(content).findall("channel/item"):
        title = node.findtext("title") or ""
        if not title.strip():
            continue
        published = node.findtext("pubDate") or ""
        try:
            when = email.utils.parsedate_to_datetime(published)
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        out.append((title, node.findtext("description") or "", node.findtext("link") or "",
                    published, when))
    return out


def _fetch_feed(url: str) -> list[tuple[str, str, str, str, dt.datetime]]:
    """One feed over the network; fills the cache. Raises on failure; the caller swallows it.
    Its own client, so a search returning at the deadline cannot close it under a straggler."""
    try:
        with httpx.Client(timeout=FEED_TIMEOUT_S, follow_redirects=False,
                          headers={"User-Agent": _UA}) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return []
        items = _parse_feed(resp.content)
        _cache[url] = (time.monotonic(), items)
        return items
    finally:
        _inflight.pop(url, None)


def _start(url: str) -> concurrent.futures.Future:
    """A future for this feed: already resolved from a fresh cache, the fetch already in
    flight, or a new one."""
    cached = _cache.get(url)
    if cached is not None and time.monotonic() - cached[0] < FEED_CACHE_S:
        done: concurrent.futures.Future = concurrent.futures.Future()
        done.set_result(cached[1])
        return done
    future = _inflight.get(url)
    if future is None:
        future = _inflight[url] = _pool.submit(_fetch_feed, url)
    return future


def prefetch() -> None:
    """Warm the cache at server start, so the opening search finds headlines already there."""
    for _name, url in _active_feeds():
        _start(url)


def _feed_items(query: str) -> dict[str, list[dict[str, str]]]:
    """Fresh headlines from every feed, grouped by PROVIDER, ranked, never raising.

    All feeds are fetched at once and the answer is cut off at FEEDS_DEADLINE_S, so the slowest
    feed cannot hold up the answer; a late feed still lands in the cache for next time. Ranking puts headlines sharing a word with the query first,
    then newest first; a generic opening query ("日本 ニュース") matches nothing and simply gets
    today's top stories, which is what an opening wants. One broken source never costs the others.
    """
    feeds = _active_feeds()
    if not feeds:
        return {}
    terms = [t for t in _WS.split(query) if len(t) >= 2]
    now = dt.datetime.now(dt.timezone.utc)
    fetched: dict[str, list] = {}
    futures = {_start(url): name for name, url in feeds}
    # Stragglers are not waited for and not cancelled: they finish into the cache.
    done, _late = concurrent.futures.wait(futures, timeout=FEEDS_DEADLINE_S)
    for future in done:
        try:
            fetched[futures[future]] = future.result()
        except (httpx.HTTPError, ET.ParseError, ValueError):
            pass

    grouped: dict[str, list[tuple[int, float, dict[str, str]]]] = {}
    for name, _url in feeds:                       # feed-file order, so providers keep theirs
        provider = name.split("/", 1)[0]
        for title, desc, link, published, when in fetched.get(name, []):
            age_h = (now - when).total_seconds() / 3600.0
            if age_h > MAX_AGE_HOURS:
                continue
            clean_title = _clean(title, MAX_TITLE)
            if not clean_title:
                continue
            score = sum(1 for t in terms if t in title + " " + desc)
            grouped.setdefault(provider, []).append((score, age_h, {
                "title": clean_title,
                "snippet": _clean(desc, MAX_SNIPPET),
                "url": _clean(link, 200),
                "published": _clean(published, 40),
                "source": provider,
            }))
    out: dict[str, list[dict[str, str]]] = {}
    for provider, rows in grouped.items():
        rows.sort(key=lambda r: (-r[0], r[1]))
        seen: set[str] = set()
        kept = []
        for _, _, item in rows:
            if item["title"] not in seen:
                seen.add(item["title"])
                kept.append(item)
            if len(kept) >= PER_PROVIDER:
                break
        out[provider] = kept
    return out


def _interleave(buckets: dict[str, list[dict[str, str]]], limit: int) -> list[dict[str, str]]:
    """Round-robin across sources, de-duplicated by title.

    Measured 2026-09-10: 47 of 57 results on a Japanese news query came from brave.news alone.
    Taking SearxNG's first N meant the tutor effectively had one source. One from each, in turn.
    """
    queues = [list(rows) for rows in buckets.values() if rows]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    while queues and len(out) < limit:
        for queue in queues:
            while queue:
                item = queue.pop(0)
                if item["title"] not in seen:
                    seen.add(item["title"])
                    out.append(item)
                    break
            if len(out) >= limit:
                break
        queues = [q for q in queues if q]
    return out


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

    # SearxNG first. A failure here is recorded, not returned: for `news` the headline feeds can
    # still answer, and "SearxNG is down" should cost the tutor one source, not all of them.
    searx_error: dict[str, str] | None = None
    payload: dict[str, Any] = {}
    try:
        with httpx.Client(base_url=_base(), timeout=TIMEOUT_S, follow_redirects=False) as client:
            resp = client.get("/search", params=params)
        if resp.status_code == 403:
            searx_error = {"error": "SearxNG rejected the request (403)",
                           "hint": "settings.yml needs `json` in search.formats and server.limiter: false."}
        else:
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        searx_error = {"error": f"{type(exc).__name__}: {exc}"[:200],
                       "hint": "Search is unavailable. Open from what you already know about the student instead."}

    buckets: dict[str, list[dict[str, str]]] = {}
    if category == "news":
        buckets.update(_feed_items(query))
    for row in (payload.get("results") or [])[:40]:
        if not isinstance(row, dict):
            continue
        title = _clean(row.get("title"), MAX_TITLE)
        if not title:
            continue
        engine = _clean(row.get("engine"), 40) or "web"
        buckets.setdefault(engine, []).append({
            "title": title,
            "snippet": _clean(row.get("content"), MAX_SNIPPET),
            "url": _clean(row.get("url"), 200),
            "published": _clean(row.get("publishedDate") or row.get("pubdate"), 40),
            "source": engine,
        })

    results = _interleave(buckets, MAX_RESULTS)
    if not results and searx_error:
        return searx_error
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
    print("search-mcp: read-only stdio server, 1 tool (SearxNG + headline feeds)", file=sys.stderr)
    prefetch()
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
