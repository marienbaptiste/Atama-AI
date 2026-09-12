"""Spec §5 / ADR-021 standing test: a full mocked session — launch fetch of BOTH services through
profile.build(), then every Bunpro MCP tool — with every outgoing HTTP request recorded. All of
them are GET, all go to the two pinned hosts, and the client has nothing to write with.

Two recorders: the SrsClient's own transport (what the fetchers send), and the real
`httpx.HTTPTransport`, patched to record and refuse — so anything that bypassed the mock (the MCP
server included, which must make no request at all) would show up here instead of on the wire.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from backend import constants
from backend.srs import bunpro_mcp as m
from backend.srs import profile as profile_api
from backend.srs.http import ALLOWED_HOSTS, SrsClient
from backend.status import StatusRegistry

FX = Path(__file__).parent / "fixtures"
PINNED_HOSTS = {httpx.URL(constants.WANIKANI_ORIGIN).host, httpx.URL(constants.BUNPRO_ORIGIN).host}


def fx(service, name):
    return json.loads((FX / service / f"{name}.json").read_text(encoding="utf-8"))


_BUNPRO = {"user": "user", "due": "due", "jlpt_progress_mixed": "jlpt_progress", "srs_ghost_level_details": "ghost_grammar",
           "srs_level_details": "srs_level_beginner_grammar", "forecast_daily": "forecast_daily"}
_WANIKANI = {"/v2/user": "user", "/v2/summary": "summary", "/v2/assignments": "assignments_vocab",
             "/v2/review_statistics": "review_statistics_vocab", "/v2/subjects": "subjects_recent"}


def recorder(requests: list[httpx.Request]):
    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.url.host == httpx.URL(constants.BUNPRO_ORIGIN).host:
            return httpx.Response(200, json=fx("bunpro", _BUNPRO[req.url.path.rsplit("/", 1)[-1]]))
        name = _WANIKANI.get(req.url.path)
        if name == "assignments_vocab" and req.url.params.get("subject_types") == "kanji":
            return httpx.Response(200, json={"data": [], "pages": {"next_url": None}})
        return httpx.Response(200, json=fx("wanikani", name) if name else {"data": []})
    return handler


def _payload(result):
    if isinstance(result, tuple):
        return result[1] if isinstance(result[1], dict) else json.loads(result[0][0].text)
    sc = getattr(result, "structuredContent", None)
    return sc.get("result", sc) if sc else json.loads(result.content[0].text)


def test_a_full_mocked_session_sends_only_get_to_the_pinned_hosts(tmp_path, monkeypatch):
    seen: list[httpx.Request] = []
    escaped: list[httpx.Request] = []

    def refuse(self, request):   # anything reaching the real transport is recorded and refused
        escaped.append(request)
        raise AssertionError(f"a request reached the network transport: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    handler = recorder(seen)
    overrides = {svc: {"transport": httpx.MockTransport(handler), "sleep": lambda s: None} for svc in ("wanikani", "bunpro")}

    # 1. Launch: both services, forced so the fetch really happens.
    reg = StatusRegistry()
    prof = profile_api.build("wk-token", "bp-token", tmp_path, 3600, 10, reg, overrides, force=True)
    assert prof.sources == {"wanikani": "ok", "bunpro": "ok"}, (prof.sources, prof.errors)
    launch_count = len(seen)
    assert launch_count >= 5 + 8   # 5 WaniKani calls + 8 Bunpro calls at least (spec §5, 2026-09-12)

    # 2. The tutor's mid-session tool calls: all three, reading the snapshot the launch wrote.
    monkeypatch.setattr(m, "_snapshot_path", tmp_path / "bunpro.json")
    for tool in m.READ_TOOLS:
        out = _payload(asyncio.run(m.server.call_tool(tool, {})))
        assert "error" not in out, (tool, out)
        assert out["synced_at"] and out["age_minutes"] is not None
    assert len(seen) == launch_count, "MCP tools must make no request at all (ADR-024)"

    # 3. Every request, every layer: GET, pinned hosts, no redirects followed, nothing escaped.
    assert seen and all(r.method == "GET" for r in seen), sorted({r.method for r in seen})
    assert {r.url.host for r in seen} == PINNED_HOSTS, sorted({r.url.host for r in seen})
    assert {r.url.host for r in seen} <= ALLOWED_HOSTS
    assert escaped == []
    # Bunpro's write-shaped verbs do not exist as paths we ever touch either.
    assert not any(seg in r.url.path for r in seen for seg in ("/update", "/create", "/submit", "/start", "/delete"))


def test_the_client_has_nothing_to_write_with():
    for name in ("post", "put", "patch", "delete", "request", "stream", "send", "build_request"):
        assert not hasattr(SrsClient, name), name
    public = sorted(n for n in vars(SrsClient) if not n.startswith("_") and callable(getattr(SrsClient, n)))
    assert public == ["get"]
    c = SrsClient("bunpro", "tok", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), sleep=lambda s: None)
    assert not any(n.startswith(("set_", "write_", "update_", "create_", "delete_", "submit_", "start_", "post_",
                                 "put_", "patch_", "mark_", "reset_", "assign_")) for n in dir(c))
    with pytest.raises(AttributeError):
        c.post  # noqa: B018 - the attribute must not exist
