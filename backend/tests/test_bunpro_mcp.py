"""Bunpro MCP server: exactly three read tools, zero network, reads the launch snapshot, reports its age."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

from backend import mcp_ready
from backend.srs import bunpro_mcp as m
from backend.srs.cache import cache_store

FX = Path(__file__).parent / "fixtures" / "bunpro"


def fx(name):
    return json.loads((FX / f"{name}.json").read_text(encoding="utf-8"))


def raw_snapshot():
    return {n: fx(n) for n in ("user", "due", "jlpt_progress", "ghost_grammar", "srs_level_beginner_grammar", "forecast_daily")}


def snapshot(tmp_path, monkeypatch, minutes_ago=5, fetched_at=None):
    """Write a snapshot and point the server at it for this test only (monkeypatch restores)."""
    ts = fetched_at or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    p = tmp_path / "bunpro.json"
    cache_store(p, {"fetched_at": ts, "service": "bunpro", "raw": raw_snapshot()})
    monkeypatch.setattr(m, "_snapshot_path", p)
    return p


def call(name, args=None):
    return asyncio.run(m.server.call_tool(name, args or {}))


def _payload(result):
    """mcp 2.x call_tool returns (content, structured) or a CallToolResult; normalise to the dict."""
    if isinstance(result, tuple):
        return result[1] if isinstance(result[1], dict) else json.loads(result[0][0].text)
    sc = getattr(result, "structuredContent", None)
    if sc:
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


def test_tool_surface_is_exactly_three_read_tools():
    names = sorted(t.name for t in asyncio.run(m.server.list_tools()))
    assert names == sorted(m.READ_TOOLS) == ["get_ghost_reviews", "get_grammar_progress", "get_review_queue"]
    m._assert_surface()


def test_module_has_no_network_client():
    import inspect
    src = inspect.getsource(m)
    assert "SrsClient" not in src and "httpx" not in src and "BUNPRO_API_TOKEN" not in src


def test_review_queue_from_snapshot(tmp_path, monkeypatch):
    snapshot(tmp_path, monkeypatch, minutes_ago=7)
    out = _payload(call("get_review_queue"))
    assert out["due_grammar"] == 0 and out["grammar_tomorrow"] == 15 and out["grammar_later"] == 15
    assert 6 <= out["age_minutes"] <= 8 and out["synced_at"]


def test_ghost_reviews_from_snapshot(tmp_path, monkeypatch):
    snapshot(tmp_path, monkeypatch)
    out = _payload(call("get_ghost_reviews"))
    assert out["count"] == 1 and out["ghosts"][0]["grammar"] == "そういう" and out["ghosts"][0]["jlpt"] == "N4"


def test_grammar_progress_from_snapshot(tmp_path, monkeypatch):
    snapshot(tmp_path, monkeypatch)
    out = _payload(call("get_grammar_progress"))
    assert out["studying"] == "N4" and out["jlpt"]["N4"]["total"] == 185 and len(out["beginner_stage"]) >= 1


def test_missing_snapshot_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_snapshot_path", tmp_path / "nope.json")
    out = _payload(call("get_review_queue"))
    assert "error" in out and "Refresh" in out["hint"]


def test_corrupt_snapshot_is_a_clear_error_not_a_crash(tmp_path, monkeypatch):
    p = tmp_path / "bunpro.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(m, "_snapshot_path", p)
    for tool in m.READ_TOOLS:
        out = _payload(call(tool))
        assert "error" in out and "Refresh" in out["hint"], tool


def test_a_naive_fetched_at_degrades_the_age_not_the_answer(tmp_path, monkeypatch):
    """A legacy or hand-edited snapshot without a UTC offset: aware minus naive raises TypeError.
    Every tool used to answer `error`; the data is fine, only the age is unknown."""
    snapshot(tmp_path, monkeypatch, fetched_at="2026-09-09T09:00:00")
    for tool in m.READ_TOOLS:
        out = _payload(call(tool))
        assert "error" not in out, tool
        assert out["age_minutes"] is None and out["synced_at"] == "2026-09-09T09:00:00"
    assert _payload(call("get_ghost_reviews"))["ghosts"][0]["grammar"] == "そういう"


def test_missing_env_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(m, "_snapshot_path", None)
    monkeypatch.delenv("ATAMA_SNAPSHOT", raising=False)
    out = _payload(call("get_ghost_reviews"))
    assert "ATAMA_SNAPSHOT" in out["error"]


def test_ready_marker_written_by_initialized_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_ready_path", tmp_path / "bunpro_mcp.ready")
    assert not m._ready_path.exists()
    mcp_ready.announce_ready(m._marker())
    data = json.loads(m._ready_path.read_text(encoding="utf-8"))
    assert data["pid"] > 0 and data["connected_at"]
    mcp_ready.clear_marker(m._marker())
    assert not m._ready_path.exists()


def test_stdio_handshake_writes_marker_and_lists_tools(tmp_path, monkeypatch):
    """Real subprocess + real MCP initialize over stdio (no network). The marker must appear
    only once the client has initialized — this is the signal the orchestrator waits on."""
    import os
    import sys
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    snap = snapshot(tmp_path, monkeypatch)
    marker = tmp_path / "ready"
    repo = str(Path(__file__).resolve().parents[2])
    env = {**os.environ, "ATAMA_SNAPSHOT": str(snap), "ATAMA_MCP_READY": str(marker), "PYTHONPATH": repo}
    params = StdioServerParameters(command=sys.executable, args=["-m", "backend.srs.bunpro_mcp"], env=env, cwd=repo)

    async def run():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for _ in range(100):  # the notification handler runs right after initialize; allow scheduling
                    if marker.exists():
                        break
                    await asyncio.sleep(0.02)
                assert marker.exists(), "ready marker not written after initialize"
                tools = await s.list_tools()
                res = await s.call_tool("get_ghost_reviews", {})
                return sorted(t.name for t in tools.tools), res.content[0].text

    names, text = asyncio.run(run())
    assert names == sorted(m.READ_TOOLS)
    assert "そういう" in text
    assert not marker.exists(), "marker must be removed when the server exits"
