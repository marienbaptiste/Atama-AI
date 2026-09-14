"""Spec §5 / ADR-021 standing test: a full mocked session — launch fetch of BOTH services through
profile.build(), then the student's manual Refresh — with every outgoing HTTP request recorded. All
of them are GET, all go to the two pinned hosts, and the client has nothing to write with.

Two recorders: the SrsClient's own transport (what the fetchers send), and the real
`httpx.HTTPTransport`, patched to record and refuse — so anything that bypassed the mock would show
up here instead of on the wire.

Until 2026-09-14 step 2 called the three Bunpro MCP tools; that server is retired (ADR-039), and
the last test here keeps it from quietly coming back as a model-facing tool on SRS data.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest

from backend import constants
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

    # 2. The student presses Refresh (ADR-024's only other caller): the same fetch, forced again.
    again = profile_api.build("wk-token", "bp-token", tmp_path, 3600, 10, reg, overrides, force=True)
    assert again.sources == {"wanikani": "ok", "bunpro": "ok"}, (again.sources, again.errors)
    assert len(seen) == 2 * launch_count, "a Refresh is exactly one more launch fetch, nothing else"

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


def _tool_server_imports(source: str) -> list[str]:
    """Every import in `source` that would make it an MCP server: the `mcp` library, or our own
    readiness helper (`backend.mcp_ready`), however it is spelled."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [f"{node.module or ''}.{a.name}" for a in node.names]
        else:
            continue
        found += [n for n in names if n == "mcp" or n.startswith("mcp.") or n.split(".")[-1] == "mcp_ready"]
    return found


def test_the_tool_server_check_catches_every_spelling():
    assert _tool_server_imports("import mcp") == ["mcp"]
    assert _tool_server_imports("from mcp.server.mcpserver import MCPServer")
    assert _tool_server_imports("from backend import mcp_ready")
    assert _tool_server_imports("import backend.mcp_ready")
    assert _tool_server_imports("from backend.srs import bunpro\nimport httpx") == []


def test_no_srs_module_is_a_tool_server():
    """ADR-039: WaniKani and Bunpro reach the tutor through the profile only. Nothing under srs/
    may import an MCP library or build a server a model could call."""
    srs = Path(__file__).resolve().parents[1] / "srs"
    offenders = {path.name: hits for path in sorted(srs.rglob("*.py"))
                 if (hits := _tool_server_imports(path.read_text(encoding="utf-8")))}
    assert offenders == {}
    assert not (srs / "bunpro_mcp.py").exists()
