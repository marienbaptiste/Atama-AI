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


def test_every_sentence_carries_its_turn_and_a_bargein_closes_it():
    """Spec §8 barge-in: the page drops audio of an interrupted turn however late it arrives, and
    never mistakes the next turn's for it. Before 2026-09-11 every sentence said turn 0 and the
    server never sent `bargein` at all."""
    hub, sent = app.Hub(), []

    async def capture(message, to=None):
        sent.append(message)

    hub.send = capture

    async def go():
        await hub.state("thinking")          # a turn starts
        await hub.speak(FakeSpeech())
        await hub.bargein()                  # the student talks over her
        await hub.speak(FakeSpeech())        # anything after the barge-in is a newer epoch
        await hub.state("thinking")          # the next turn
        await hub.speak(FakeSpeech())

    asyncio.run(go())
    assert [(m["type"], m["turn"]) for m in sent] == [
        ("state", 1), ("speak", 1), ("bargein", 1), ("speak", 2), ("state", 3), ("speak", 3)]


def test_study_marks_ride_with_their_sentence():
    """ADR-036: the grammar she used and the form she wants reach the page with the audio."""
    from backend.chunker import GrammarMark
    hub, sent = app.Hub(), []

    async def capture(message, to=None):
        sent.append(message)

    hub.send = capture
    speech = FakeSpeech()
    speech.grammar, speech.target = (GrammarMark(0, 2, "〜たら"),), "〜たら"
    asyncio.run(hub.speak(speech))
    asyncio.run(hub.speak(FakeSpeech()))
    assert sent[0]["grammar"] == [{"start": 0, "end": 2, "point": "〜たら"}] and sent[0]["target"] == "〜たら"
    assert sent[1]["grammar"] == [] and sent[1]["target"] == ""


def test_the_root_is_the_built_page_or_says_how_to_build_it(tmp_path, monkeypatch):
    """One page (the prototype was removed, 2026-09-11): / serves the build, or explains itself."""
    monkeypatch.setattr(app, "DIST_DIR", tmp_path)
    client = TestClient(app.build(app.Hub()))
    missing = client.get("/")
    assert missing.status_code == 503 and "not built" in missing.text
    (tmp_path / "index.html").write_text("<title>built</title>", encoding="utf-8")
    assert client.get("/").text == "<title>built</title>"
    assert client.get("/preview.html").status_code == 404
    assert app.page_url("127.0.0.1", 8000) == "http://127.0.0.1:8000/"


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
    asyncio.run(hub.meters(context_tokens=6204, context_window=200000, month_turns=2))
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


def test_service_changes_reach_the_pages_from_any_thread_and_on_a_heartbeat():
    """Spec §5b / ROADMAP 16: on every change — reported from whatever thread noticed — and at
    least every STATUS_HEARTBEAT_S, changed or not."""
    import threading
    from backend.status import StatusRegistry

    reg = StatusRegistry()
    reg.report("voicevox", "warm", "engine 0.25.2")
    hub, sent = app.Hub(), []

    async def capture(message, to=None):
        sent.append((message["service"], message["state"]) if message.get("type") == "service_status" else None)

    hub.send = capture

    async def run():
        loop = asyncio.get_running_loop()
        hub.follow(reg, loop)                                   # replays what is known now
        await asyncio.sleep(0.01)
        t = threading.Thread(target=lambda: reg.report("wanikani", "stale", "serving cache"))
        t.start(); t.join()                                     # a report from another thread
        await asyncio.sleep(0.05)
        beat = asyncio.create_task(hub.status_heartbeat(reg, 0.02))
        await asyncio.sleep(0.05)
        beat.cancel()

    asyncio.run(run())
    assert ("voicevox", "warm") in sent and ("wanikani", "stale") in sent
    assert sent.count(("voicevox", "warm")) >= 2                # the heartbeat re-sent it unchanged
    assert hub.last_status["wanikani"]["state"] == "stale"      # and a late page will see it


def test_the_page_asks_for_a_full_stop_not_a_pause():
    """The stop button means "I am done for today": the REPL reads `quit_requested` and takes the
    containers down too, which Ctrl+C deliberately does not (user, 2026-09-12)."""
    import asyncio

    from backend import repl

    hub = app.Hub()
    assert hub.quit_requested is False

    stopped = []
    asyncio.run(repl._stop_containers(hub))          # nobody pressed it: nothing happens
    assert stopped == []

    hub.quit_requested = True
    import backend.tools.down as down
    real, down.main = down.main, lambda argv: stopped.append(argv) or 0
    try:
        asyncio.run(repl._stop_containers(hub))
    finally:
        down.main = real
    assert stopped == [[]]
