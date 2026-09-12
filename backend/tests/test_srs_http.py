"""SrsClient: GET-only, host-allowlisted, redirect-free, token never leaks (spec §0, ADR-021/023)."""
from __future__ import annotations

import logging
import threading

import httpx
import pytest

from backend import constants
from backend.srs import http as srs_http
from backend.srs.http import HostViolation, ReadOnlyTransport, ReadOnlyViolation, SrsClient, SrsError

TOKEN = "supersecrettoken1234"


def mock(handler):
    return httpx.MockTransport(handler)


def test_public_surface_is_exactly_get():
    public = sorted(n for n, v in vars(SrsClient).items() if not n.startswith("_") and callable(v))
    assert public == ["get"]
    for name in ("post", "put", "patch", "delete", "request"):
        assert not hasattr(SrsClient, name)


def test_get_sends_bunpro_token_header_and_returns_json():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["Authorization"]
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"ok": True})

    c = SrsClient("bunpro", TOKEN, transport=mock(handler), sleep=lambda s: None)
    assert c.get("/api/frontend/user") == {"ok": True}
    assert seen["auth"] == f"Token token={TOKEN}"
    # Verified live: the Account API Token is only honoured with this opt-in parameter.
    assert seen["url"] == constants.BUNPRO_ORIGIN + f"/api/frontend/user?{constants.BUNPRO_TOKEN_OPT_IN_PARAM}=true"


def test_bunpro_opt_in_param_merges_with_caller_params():
    seen = {}

    def handler(req):
        seen["q"] = dict(req.url.params)
        seen["origin"] = req.headers.get("Origin")
        return httpx.Response(200, json={})

    c = SrsClient("bunpro", TOKEN, transport=mock(handler), sleep=lambda s: None)
    c.get("/api/frontend/user_stats/srs_level_details", {"reviewable_type": "Grammar", "level": 1})
    assert seen["q"] == {constants.BUNPRO_TOKEN_OPT_IN_PARAM: "true", "reviewable_type": "Grammar", "level": "1"}
    assert seen["origin"] == constants.BUNPRO_SITE_ORIGIN


def test_wanikani_has_no_opt_in_param():
    seen = {}
    c = SrsClient("wanikani", TOKEN, transport=mock(lambda r: seen.__setitem__("url", str(r.url)) or httpx.Response(200, json={})), sleep=lambda s: None)
    c.get("/v2/user")
    assert seen["url"] == constants.WANIKANI_ORIGIN + "/v2/user"


def test_get_sends_wanikani_bearer_and_revision():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["Authorization"]
        seen["rev"] = req.headers["Wanikani-Revision"]
        return httpx.Response(200, json={"data": {}})

    c = SrsClient("wanikani", TOKEN, transport=mock(handler), sleep=lambda s: None)
    c.get("/v2/user")
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["rev"] == constants.WANIKANI_REVISION


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_transport_refuses_non_get_even_when_bypassing_get(method):
    """Hand the guard a raw Request (bypassing SrsClient.get entirely): it must refuse before sending."""
    sent = []
    c = SrsClient("bunpro", TOKEN, transport=mock(lambda r: sent.append(r) or httpx.Response(200)), sleep=lambda s: None)
    guard = c._http._transport
    req = httpx.Request(method, constants.BUNPRO_ORIGIN + "/api/frontend/reviews/1/update", json={})
    with pytest.raises(ReadOnlyViolation):
        guard.handle_request(req)
    assert sent == []


def test_transport_refuses_foreign_host():
    sent = []
    c = SrsClient("bunpro", TOKEN, transport=mock(lambda r: sent.append(r) or httpx.Response(200)), sleep=lambda s: None)
    with pytest.raises(HostViolation):
        c._http.get("https://example.com/steal")
    with pytest.raises(HostViolation):
        c._http._transport.handle_request(httpx.Request("GET", "https://bunpro.jp.evil.example/api/frontend/user"))
    assert sent == []


def test_redirect_is_not_followed():
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(302, headers={"Location": "https://evil.example/x"})

    c = SrsClient("bunpro", TOKEN, transport=mock(handler), sleep=lambda s: None)
    with pytest.raises(SrsError) as ei:
        c.get("/api/frontend/user")
    assert ei.value.status_code == 302
    assert calls == [constants.BUNPRO_ORIGIN + f"/api/frontend/user?{constants.BUNPRO_TOKEN_OPT_IN_PARAM}=true"]


def test_errors_never_contain_token():
    def handler(req):
        return httpx.Response(401, text=f"bad token {TOKEN} rejected")

    c = SrsClient("bunpro", TOKEN, transport=mock(handler), sleep=lambda s: None)
    with pytest.raises(SrsError) as ei:
        c.get("/api/frontend/user")
    assert TOKEN not in str(ei.value)
    assert ei.value.status_code == 401


def test_transport_error_is_sanitised():
    def handler(req):
        raise httpx.ConnectError(f"boom {TOKEN}")

    c = SrsClient("bunpro", TOKEN, transport=mock(handler), sleep=lambda s: None)
    with pytest.raises(SrsError) as ei:
        c.get("/api/frontend/user")
    assert TOKEN not in str(ei.value)


def test_throttle_spaces_requests():
    t = {"now": 0.0}
    slept = []
    c = SrsClient("bunpro", TOKEN, transport=mock(lambda r: httpx.Response(200, json={})),
                  clock=lambda: t["now"], sleep=lambda s: (slept.append(s), t.__setitem__("now", t["now"] + s)))
    c.get("/api/frontend/user")
    c.get("/api/frontend/user/due")
    assert slept and abs(slept[0] - constants.BUNPRO_MIN_INTERVAL_S) < 1e-6


def test_origins_are_constants_not_env(monkeypatch):
    monkeypatch.setenv("BUNPRO_ORIGIN", "https://example.com")
    c = SrsClient("bunpro", TOKEN, transport=mock(lambda r: httpx.Response(200, json={})), sleep=lambda s: None)
    assert str(c._http.base_url).rstrip("/") == constants.BUNPRO_ORIGIN
    assert srs_http.ALLOWED_HOSTS == {"api.wanikani.com", "api.bunpro.jp"}


def test_relative_path_required():
    c = SrsClient("bunpro", TOKEN, transport=mock(lambda r: httpx.Response(200, json={})), sleep=lambda s: None)
    with pytest.raises(ValueError):
        c.get("api/frontend/user")


def test_the_guard_is_the_read_only_transport_the_spec_names():
    c = SrsClient("bunpro", TOKEN, transport=mock(lambda r: httpx.Response(200, json={})), sleep=lambda s: None)
    assert isinstance(c._http._transport, ReadOnlyTransport)
    # The gate (rule 4) asserts the older name; it is the same guard, not a second one.
    assert issubclass(srs_http._GuardTransport, ReadOnlyTransport)
    assert srs_http._GuardTransport.handle_request is ReadOnlyTransport.handle_request


def test_a_refusal_is_logged_critical(caplog):
    """spec 0: the request never leaves the process and the refusal is logged as CRITICAL."""
    c = SrsClient("wanikani", TOKEN, transport=mock(lambda r: httpx.Response(200)), sleep=lambda s: None)
    with caplog.at_level(logging.CRITICAL, logger="backend.srs.http"):
        with pytest.raises(ReadOnlyViolation):
            c._http._transport.handle_request(httpx.Request("PUT", constants.WANIKANI_ORIGIN + "/v2/user"))
        with pytest.raises(HostViolation):
            c._http._transport.handle_request(httpx.Request("GET", "https://example.com/x"))
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(msgs) == 2 and "refused PUT api.wanikani.com/v2/user" in msgs[0] and "example.com" in msgs[1]
    assert all(TOKEN not in m for m in msgs)


def test_429_ends_the_fetch_for_this_client_without_retry():
    sent = []

    def handler(req):
        sent.append(req.url.path)
        return httpx.Response(429, headers={"Retry-After": "120"}, text=f"slow down {TOKEN}")

    c = SrsClient("wanikani", TOKEN, transport=mock(handler), sleep=lambda s: None)
    assert c.rate_limited is None
    with pytest.raises(SrsError) as ei:
        c.get("/v2/assignments")
    assert ei.value.status_code == 429 and "Retry-After 120" in str(ei.value) and TOKEN not in str(ei.value)
    with pytest.raises(SrsError) as ei2:      # the next call is refused before it is sent
        c.get("/v2/summary")
    assert ei2.value.status_code == 429 and "no retry" in str(ei2.value)
    assert sent == ["/v2/assignments"]
    assert c.rate_limited == str(ei.value)


def test_cancel_flag_refuses_the_next_request_before_it_is_sent():
    sent = []
    cancel = threading.Event()
    c = SrsClient("bunpro", TOKEN, transport=mock(lambda r: sent.append(r.url.path) or httpx.Response(200, json={})),
                  sleep=lambda s: None, cancel=cancel)
    c.get("/api/frontend/user")
    cancel.set()
    with pytest.raises(SrsError) as ei:
        c.get("/api/frontend/user/due")
    assert "cancelled" in str(ei.value) and sent == ["/api/frontend/user"]
