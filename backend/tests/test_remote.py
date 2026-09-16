"""The phone page (ADR-041): the certificate, the address rules, the key, the QR, the listener's
start and stop against a fake uvicorn, and the admission rules in the hub."""
from __future__ import annotations

import asyncio
import types

import pytest
from starlette.testclient import TestClient

from backend import app, config, remote, settings_view


# ----------------------------------------------------------------- helpers
def test_the_address_must_be_one_of_this_machines_and_never_a_wildcard():
    assert remote.bad_host("192.168.1.20") == ""
    assert "wildcard" in remote.bad_host("0.0.0.0")
    assert "wildcard" in remote.bad_host("::")
    assert "loopback" in remote.bad_host("127.0.0.1")
    assert "IP address" in remote.bad_host("my-laptop")
    assert "private" in remote.bad_host("8.8.8.8")                    # a public address: never
    assert remote.bad_host("10.0.0.5") == "" and remote.bad_host("172.20.1.9") == ""
    assert remote.is_private_client("192.168.1.9") and remote.is_private_client("fe80::1")
    assert not remote.is_private_client("8.8.8.8") and not remote.is_private_client("127.0.0.1")
    assert not remote.is_private_client(None)


def test_the_certificate_is_made_once_and_renewed_when_the_address_changes(tmp_path):
    cert, key, fp = remote.certificate(tmp_path, "192.168.1.20")
    assert cert.exists() and key.exists() and len(fp) == 95      # 32 bytes as AB:CD:...
    assert remote.certificate(tmp_path, "192.168.1.20")[2] == fp   # reused
    assert remote.certificate(tmp_path, "10.0.0.7")[2] != fp       # a new address: a new one


def test_the_url_carries_the_key_and_the_qr_encodes_it():
    key = remote.new_key()
    assert len(key) >= 20
    url = remote.page_url("192.168.1.20", 8443, key)
    assert url == f"https://192.168.1.20:8443/?k={key}"
    svg = remote.qr_svg(url)
    assert svg.startswith("<svg") and "path" in svg


# ----------------------------------------------------------------- the listener
class FakeHub:
    def __init__(self):
        self.remote_key = None
        self.remote_origin = None


def cfg_like(tmp_path, **over):
    values = {"REMOTE_ENABLED": True, "REMOTE_HOST": "192.168.1.20", "REMOTE_PORT": 8443,
              "PORT": 8000, "CLAUDE_CWD": str(tmp_path / "cwd")}
    values.update(over)
    return types.SimpleNamespace(**values, get=lambda k: values[k])


@pytest.fixture
def served(monkeypatch, tmp_path):
    """No sockets: `_serve` is replaced. Nothing may touch settings.json: the key is per run."""
    monkeypatch.setattr(config, "save", lambda *a, **k: pytest.fail("the session key must never be saved"))
    started = []

    async def fake_serve(self, hub, cfg, host, port, cert, keyfile):
        started.append((host, port, cert.exists(), keyfile.exists()))
        self.task = asyncio.get_running_loop().create_future()

    monkeypatch.setattr(remote.RemoteServer, "_serve", fake_serve)

    async def fake_shutdown(task):
        task.cancel()

    monkeypatch.setattr("backend.app.shutdown", fake_shutdown)
    return started


def test_apply_starts_with_this_runs_session_key_and_stops_when_turned_off(served, tmp_path):
    started = served

    async def run():
        hub, server = FakeHub(), remote.RemoteServer()
        await server.apply(hub, cfg_like(tmp_path))
        assert server.running and server.error == ""
        assert started == [("192.168.1.20", 8443, True, True)]
        assert server.key and hub.remote_key == server.key
        assert remote.RemoteServer().key != server.key             # every run its own
        assert hub.remote_origin == "https://192.168.1.20:8443"
        info = server.info(cfg_like(tmp_path))
        assert info["enabled"] and info["url"].endswith("?k=" + server.key) and info["qr_svg"]
        old = server.rotate()
        assert old == server.key and old != hub.remote_key             # served from the next start
        await server.apply(hub, cfg_like(tmp_path))                 # unchanged: nothing restarted
        assert len(started) == 1
        await server.apply(hub, cfg_like(tmp_path, REMOTE_ENABLED=False))
        assert not server.running and hub.remote_key is None and hub.remote_origin is None
        assert server.info(cfg_like(tmp_path, REMOTE_ENABLED=False))["url"] == ""
    asyncio.run(run())


def test_a_bad_address_is_an_error_on_the_card_not_an_exception(served, tmp_path):
    async def run():
        hub, server = FakeHub(), remote.RemoteServer()
        await server.apply(hub, cfg_like(tmp_path, REMOTE_HOST="0.0.0.0"))
        assert not server.running and "wildcard" in server.error
        assert "wildcard" in server.info(cfg_like(tmp_path, REMOTE_HOST="0.0.0.0"))["detail"]
        assert hub.remote_key is None
    asyncio.run(run())


def test_a_listener_that_will_not_start_is_reported(monkeypatch, tmp_path):

    async def boom(self, *a):
        raise RuntimeError("port in use")

    monkeypatch.setattr(remote.RemoteServer, "_serve", boom)

    async def run():
        hub, server = FakeHub(), remote.RemoteServer()
        await server.apply(hub, cfg_like(tmp_path))
        assert not server.running and "port in use" in server.error
    asyncio.run(run())


# ----------------------------------------------------------------- admission (backend/app.py)
PHONE = ("192.168.1.9", 40000)


@pytest.fixture
def masked(monkeypatch):
    monkeypatch.setattr(app.settings_view, "snapshot",
                        lambda *a, **k: {"values": {}, "fields": [], "pinned": {}})


def phone_client(hub):
    return TestClient(app.build(hub), client=PHONE)


def test_off_loopback_nothing_gets_in_while_the_phone_page_is_off(masked):
    hub = app.Hub()
    with phone_client(hub) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws?k=anything", headers={"origin": "https://192.168.1.20:8443"}):
                pass


def test_the_phone_needs_the_listeners_origin_and_the_key(masked):
    hub = app.Hub()
    hub.remote_origin, hub.remote_key = "https://192.168.1.20:8443", "secret-key"
    hub.remote_info = lambda: {"enabled": True, "url": "https://x/?k=secret-key", "qr_svg": "<svg/>",
                               "fingerprint": "AA", "detail": "serving"}
    with phone_client(hub) as client:
        for path, origin in (("/ws", "https://192.168.1.20:8443"),               # no key
                             ("/ws?k=wrong", "https://192.168.1.20:8443"),       # wrong key
                             ("/ws?k=secret-key", "http://192.168.1.20:8000"),   # not the TLS origin
                             ("/ws?k=secret-key", None)):                        # no origin at all
            headers = {"origin": origin} if origin else {}
            with pytest.raises(Exception):
                with client.websocket_connect(path, headers=headers):
                    pass
        with client.websocket_connect("/ws?k=secret-key", headers={"origin": "https://192.168.1.20:8443"}) as ws:
            assert ws.receive_json()["type"] == "settings"
            card = ws.receive_json()
            assert card["type"] == "remote" and card["url"] == "" and card["qr_svg"] == ""   # never the code
            assert "computer" in card["detail"]


def test_a_client_off_the_private_network_is_refused_even_with_the_key(masked):
    hub = app.Hub()
    hub.remote_origin, hub.remote_key = "https://192.168.1.20:8443", "secret-key"
    # A globally routable address (Python 3.13's `is_private` follows the IANA registry, so the
    # documentation ranges count as private there — hence a real public one).
    with TestClient(app.build(hub), client=("8.8.8.8", 40000)) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws?k=secret-key", headers={"origin": "https://192.168.1.20:8443"}):
                pass


def test_a_phone_may_not_change_secrets_or_the_remote_settings(masked, monkeypatch):
    applied = []
    monkeypatch.setattr(app.settings_view, "apply",
                        lambda values, *a, **k: applied.append(values) or {"values": {}, "fields": [], "pinned": {},
                                                                            "saved": sorted(values), "errors": {}})
    hub = app.Hub()
    hub.remote_origin, hub.remote_key = "https://192.168.1.20:8443", "secret-key"
    with phone_client(hub) as client:
        with client.websocket_connect("/ws?k=secret-key", headers={"origin": "https://192.168.1.20:8443"}) as ws:
            ws.receive_json()
            ws.send_json({"type": "settings", "values": {"SUBTITLES": "off", "WANIKANI_TOKEN": "x",
                                                         "REMOTE_ENABLED": False, "REMOTE_PORT": 1}})
            refused = ws.receive_json()
            assert refused["type"] == "error" and "WANIKANI_TOKEN" in refused["message"] \
                and "REMOTE_ENABLED" in refused["message"] and "computer" in refused["message"]
            assert ws.receive_json()["type"] == "settings"
    assert applied == [{"SUBTITLES": "off"}]


def test_for_phone_names_what_it_refuses():
    allowed, refused = settings_view.for_phone({"TURN_MODE": "vad", "BUNPRO_API_TOKEN": "t", "REMOTE_PORT": 1})
    assert allowed == {"TURN_MODE": "vad"} and refused == ["BUNPRO_API_TOKEN", "REMOTE_PORT"]


def test_the_code_reaches_loopback_pages_only(masked):
    async def run():
        hub = app.Hub()
        hub.remote_info = lambda: {"enabled": True, "url": "https://x/?k=k", "qr_svg": "<svg/>",
                                   "fingerprint": "AA", "detail": "serving"}
        local, phone = Page(), Page()
        hub.attach(local); hub.attach(phone)
        hub._remote.add(phone)
        await hub.push_remote()
        await asyncio.sleep(0.05)
        assert local.got == ["remote"] and phone.got == []
    asyncio.run(run())


class Page:
    def __init__(self):
        self.got = []

    async def send_json(self, message):
        self.got.append(message["type"])

    async def close(self, code=1000):
        pass
