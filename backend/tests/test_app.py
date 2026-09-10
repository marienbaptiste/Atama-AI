"""The page link (spec §8): no zombie sockets, no socket killed by a handler bug, a heartbeat.

Found 2026-09-10: after a microphone replug the backend recovered, but the page stayed on
"disconnected" and heard nothing more. `Hub.send` dropped a page on a failed send while leaving
its socket open, so the page never knew to reconnect.
"""
from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from backend import app


class DeadSocket:
    def __init__(self):
        self.closed = None

    async def send_json(self, message):
        raise RuntimeError("boom")

    async def close(self, code=1000):
        self.closed = code


def test_a_failed_send_closes_the_socket_so_the_page_reconnects():
    hub, ws = app.Hub(), DeadSocket()
    hub._clients.add(ws)
    asyncio.run(hub.send({"type": "state", "state": "listening"}))
    assert ws not in hub._clients
    assert ws.closed == 1011                       # closed, not merely forgotten


def test_a_failing_control_handler_does_not_take_the_socket_down(monkeypatch):
    monkeypatch.setattr(app.settings_view, "snapshot",
                        lambda *a, **k: {"values": {}, "fields": [], "pinned": {}})
    hub = app.Hub()

    def explode(action):
        raise ValueError("handler bug")

    hub.on_control = explode
    with TestClient(app.build(hub)) as client, client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "settings"
        ws.send_json({"type": "control", "action": "start"})
        error = ws.receive_json()
        assert error["type"] == "error" and "handler bug" in error["message"]
        ws.send_json({"type": "control", "action": "stop"})
        assert ws.receive_json()["type"] == "error"      # still connected, still answering


def test_the_heartbeat_reaches_pages_and_is_not_a_remembered_status(monkeypatch):
    hub, sent = app.Hub(), []

    async def capture(message):
        sent.append(message)

    hub.send = capture
    monkeypatch.setattr(app, "HEARTBEAT_S", 0.01)

    async def run():
        task = asyncio.create_task(hub.heartbeat())
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(run())
    assert sent and sent[0]["service"] == "orchestrator"
    assert "orchestrator" not in hub.last_status


class FakeSpeech:
    wav, text, emotion, duration_ms = b"RIFFfake", "はい。", "", 500

    class timeline:
        @staticmethod
        def as_message():
            return {"visemes": [], "vtimes": [], "vdurations": []}


class Page:
    def __init__(self):
        self.got = []

    async def send_json(self, message):
        self.got.append(message["type"])


def test_her_voice_waits_for_a_page_that_can_play_sound():
    """2026-09-10: the opening greeting was sent before the page was touched, and the browser's
    autoplay policy swallowed all of it. Held until the page says `ready`, it is heard."""
    hub, page = app.Hub(), Page()
    hub._clients.add(page)

    async def run():
        task = asyncio.create_task(hub.speak(FakeSpeech()))
        await asyncio.sleep(0.3)
        assert page.got == []                            # connected, not started: held
        hub.mark_ready(page)
        await asyncio.wait_for(task, 2)

    asyncio.run(run())
    assert page.got == ["speak"]


def test_with_no_page_at_all_her_voice_does_not_wait():
    async def run():
        await asyncio.wait_for(app.Hub().speak(FakeSpeech()), 0.5)

    asyncio.run(run())


def test_a_page_that_leaves_is_no_longer_ready():
    hub, page = app.Hub(), Page()
    hub._clients.add(page)
    hub.mark_ready(page)
    hub.leave(page)
    assert page not in hub._ready


def test_timing_carries_the_claude_stage_and_the_rolling_p90():
    from backend import models
    sent = []
    hub = app.Hub()

    async def capture(message):
        sent.append(message)

    hub.send = capture
    asyncio.run(hub.timing(stt_ms=400, first_chunk_ms=2600, first_audio_ms=3100, total_ms=9000,
                           ttft_ms=2400, thinking_chars=306, p50_ms=3490, p90_ms=5300, turns=20))
    assert sent[0]["type"] == "timing" and sent[0]["thinking_chars"] == 306 and sent[0]["p90_ms"] == 5300
    assert "timing" in models.SERVER_TYPES


def test_meters_merge_sources_and_replay_to_a_late_page():
    """Context and use arrive after a turn, GPU on the heartbeat: a page gets the latest of each."""
    from starlette.testclient import TestClient
    from backend import models

    hub = app.Hub()
    asyncio.run(hub.meters(context_tokens=6204, context_window=200000, month_cost_usd=0.33, month_turns=2))
    asyncio.run(hub.meters(vram_used_mib=5700, vram_total_mib=16376))
    assert hub.last_meters["context_tokens"] == 6204 and hub.last_meters["vram_used_mib"] == 5700
    assert "meters" in models.SERVER_TYPES

    original = app.settings_view.snapshot
    app.settings_view.snapshot = lambda *a, **k: {"values": {}, "fields": [], "pinned": {}}
    try:
        with TestClient(app.build(hub)) as client, client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "settings"
            meters = ws.receive_json()
            assert meters["type"] == "meters" and meters["month_turns"] == 2 and meters["vram_total_mib"] == 16376
    finally:
        app.settings_view.snapshot = original
