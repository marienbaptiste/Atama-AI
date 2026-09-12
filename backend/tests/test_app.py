"""The page link (spec §8): no zombie sockets, no socket killed by a handler bug, a heartbeat,
and — since 2026-09-12 — an Origin check on the handshake, a hold that ends when its page goes,
a per-page outbox, and a listener that fails with a reason.

Found 2026-09-10: after a microphone replug the backend recovered, but the page stayed on
"disconnected" and heard nothing more. `Hub.send` dropped a page on a failed send while leaving
its socket open, so the page never knew to reconnect.
"""
from __future__ import annotations

import asyncio
import json
import re
import socket
import types

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend import app, config, models

#: The test client's address: a browser on this machine. TestClient's default ("testclient")
#: is not loopback, and without an Origin header that is what the handshake goes by.
LOOPBACK = ("127.0.0.1", 50000)
PAGE = {"origin": "http://127.0.0.1:8000"}


@pytest.fixture
def masked(monkeypatch):
    """A settings echo without touching settings.json or the audio backend."""
    monkeypatch.setattr(app.settings_view, "snapshot",
                        lambda *a, **k: {"values": {}, "fields": [], "pinned": {}})


def connect(hub: app.Hub, **kw) -> TestClient:
    return TestClient(app.build(hub), client=LOOPBACK, **kw)


class DeadSocket:
    def __init__(self):
        self.closed = None

    async def send_json(self, message):
        raise RuntimeError("boom")

    async def close(self, code=1000):
        self.closed = code


def test_a_failed_send_closes_the_socket_so_the_page_reconnects():
    hub, ws = app.Hub(), DeadSocket()
    hub.attach(ws)

    async def go():
        await hub.send({"type": "state", "state": "listening"})
        await hub.drain()

    asyncio.run(go())
    assert ws not in hub._clients
    assert ws.closed == 1011                       # closed, not merely forgotten


def test_a_failing_control_handler_does_not_take_the_socket_down(masked):
    hub = app.Hub()

    def explode(action):
        raise ValueError("handler bug")

    hub.on_control = explode
    with connect(hub) as client, client.websocket_connect("/ws", headers=PAGE) as ws:
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
    assert sent[0]["grammar"] == [{"start": 0, "end": 2, "point": "〜たら", "level": ""}] and sent[0]["target"] == "〜たら"
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
    def __init__(self, slow_s: float = 0.0):
        self.got = []
        self.slow_s = slow_s
        self.closed = None

    async def send_json(self, message):
        if self.slow_s:
            await asyncio.sleep(self.slow_s)
        self.got.append(message["type"])

    async def close(self, code=1000):
        self.closed = code


def test_her_voice_waits_for_a_page_that_can_play_sound():
    """2026-09-10: the opening greeting was sent before the page was touched, and the browser's
    autoplay policy swallowed all of it. Held until the page says `ready`, it is heard."""
    hub, page = app.Hub(), Page()
    hub.attach(page)

    async def run():
        task = asyncio.create_task(hub.speak(FakeSpeech()))
        await asyncio.sleep(0.3)
        assert page.got == []                            # connected, not started: held
        hub.mark_ready(page)
        await asyncio.wait_for(task, 2)
        await hub.drain()

    asyncio.run(run())
    assert page.got == ["speak"]


def test_with_no_page_at_all_her_voice_does_not_wait():
    async def run():
        await asyncio.wait_for(app.Hub().speak(FakeSpeech()), 0.5)

    asyncio.run(run())


def test_a_page_that_leaves_is_no_longer_ready():
    hub, page = app.Hub(), Page()
    hub.attach(page)
    hub.mark_ready(page)
    hub.leave(page)
    assert page not in hub._ready


def test_timing_carries_the_claude_stage_and_the_rolling_p90():
    sent = []
    hub = app.Hub()

    async def capture(message):
        sent.append(message)

    hub.send = capture
    asyncio.run(hub.timing(stt_ms=400, first_chunk_ms=2600, first_audio_ms=3100, total_ms=9000,
                           ttft_ms=2400, thinking_chars=306, p50_ms=3490, p90_ms=5300, turns=20))
    assert sent[0]["type"] == "timing" and sent[0]["thinking_chars"] == 306 and sent[0]["p90_ms"] == 5300
    assert "timing" in models.SERVER_TYPES


def test_meters_merge_sources_and_replay_to_a_late_page(masked):
    """Context and use arrive after a turn, GPU on the heartbeat: a page gets the latest of each."""
    hub = app.Hub()
    asyncio.run(hub.meters(context_tokens=6204, context_window=200000, month_turns=2))
    asyncio.run(hub.meters(vram_used_mib=5700, vram_total_mib=16376))
    assert hub.last_meters["context_tokens"] == 6204 and hub.last_meters["vram_used_mib"] == 5700
    assert "meters" in models.SERVER_TYPES
    with connect(hub) as client, client.websocket_connect("/ws", headers=PAGE) as ws:
        assert ws.receive_json()["type"] == "settings"
        meters = ws.receive_json()
        assert meters["type"] == "meters" and meters["month_turns"] == 2 and meters["vram_total_mib"] == 16376


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


# ---------------------------------------------------------------- the Origin check (2026-09-12)
def test_the_allowed_origins_are_this_port_and_vites_on_loopback_names_only():
    allowed = app.allowed_origins(8000)
    assert {"http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000",
            "http://127.0.0.1:5173", "http://localhost:5173"} <= allowed
    assert all(o.startswith("http://") and o.endswith((":8000", ":5173")) for o in allowed)
    assert app.origin_ok("http://localhost:8000", "127.0.0.1", allowed)
    assert app.origin_ok("HTTP://LocalHost:8000", "127.0.0.1", allowed)      # scheme and host are case-insensitive
    assert not app.origin_ok("http://localhost:8001", "127.0.0.1", allowed)  # the port is part of it
    assert not app.origin_ok("http://evil.example", "127.0.0.1", allowed)
    assert not app.origin_ok("null", "127.0.0.1", allowed)                  # a sandboxed frame
    assert app.origin_ok(None, "127.0.0.1", allowed)                        # not a browser, from here
    assert app.origin_ok(None, "::1", allowed)
    assert not app.origin_ok(None, "10.0.0.7", allowed)
    assert not app.origin_ok(None, None, allowed)


def test_the_dev_port_matches_vites_config():
    """DEV_PORT is a copy of vite.config.ts `server.port`; the two must not drift apart."""
    text = (config.REPO_ROOT / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
    assert int(re.search(r"port:\s*(\d+)", text).group(1)) == app.DEV_PORT


def test_a_foreign_origin_is_refused_before_the_handshake(masked):
    """Any web page in the same browser can open ws://127.0.0.1:8000/ws — until this. It gets no
    socket, no settings, nothing (spec §8, ADR-017)."""
    hub = app.Hub()
    with connect(hub) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"origin": "http://evil.example"}):
                pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8001"}):
                pass
    assert hub._clients == {}


def test_the_page_from_this_server_or_from_vite_is_let_in(masked):
    hub = app.Hub()
    with connect(hub) as client:
        for origin in ("http://127.0.0.1:8000", "http://localhost:5173"):
            with client.websocket_connect("/ws", headers={"origin": origin}) as ws:
                assert ws.receive_json()["type"] == "settings"


def test_a_client_with_no_origin_is_let_in_from_loopback_only(masked):
    hub = app.Hub()
    with connect(hub) as client, client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "settings"
    with TestClient(app.build(hub), client=("192.168.1.9", 40000)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws"):
                pass


# ---------------------------------------------------------------- the hold survives nothing
def test_a_page_that_drops_mid_hold_has_its_hold_cancelled():
    """The voice loop refuses a new press while one is open (`VoiceLoop.ptt_begin`). A page that
    went away holding the key used to wedge the microphone until a restart (2026-09-12)."""
    hub, page, actions = app.Hub(), Page(), []
    hub.on_control = actions.append
    hub.attach(page)
    hub.control(page, "start")
    hub.leave(page)
    assert actions == ["start", "cancel"]
    hub.leave(page)                                    # gone already: nothing more
    assert actions == ["start", "cancel"]


def test_a_hold_that_ended_is_not_cancelled_when_the_page_leaves():
    for end in ("stop", "cancel"):
        hub, page, actions = app.Hub(), Page(), []
        hub.on_control = actions.append
        hub.attach(page)
        hub.control(page, "start")
        hub.control(page, end)
        hub.leave(page)
        assert actions == ["start", end]


def test_ready_marks_the_page_and_reaches_no_handler():
    hub, page, actions = app.Hub(), Page(), []
    hub.on_control = actions.append
    hub.attach(page)
    hub.control(page, "ready")
    assert page in hub._ready and actions == []


def test_a_hold_cut_by_a_socket_drop_is_cancelled_over_the_route(masked):
    hub, actions = app.Hub(), []
    hub.on_control = actions.append
    with connect(hub) as client:
        with client.websocket_connect("/ws", headers=PAGE) as ws:
            ws.receive_json()
            ws.send_json({"type": "control", "action": "start"})
            ws.send_json({"type": "control", "action": "ready"})   # a round trip: start was handled
        # the `with` closed the socket with the key still down
    assert actions == ["start", "cancel"]


# ---------------------------------------------------------------- delivery
def test_pages_are_sent_to_concurrently_and_a_slow_one_delays_nobody():
    hub, quick, slow = app.Hub(), Page(), Page(slow_s=0.2)
    hub.attach(quick)
    hub.attach(slow)

    async def go():
        await hub.send({"type": "state", "state": "thinking", "turn": 1})
        await asyncio.sleep(0.05)
        assert quick.got == ["state"] and slow.got == []   # the quick page has it already
        await hub.drain()

    asyncio.run(go())
    assert slow.got == ["state"]


def test_a_slow_page_gets_the_latest_level_and_every_sentence():
    """mic_level arrives ten times a second; a page that cannot keep up gets the newest one, not
    a backlog. speak/state/turn are never dropped."""
    hub, page = app.Hub(), Page(slow_s=0.03)
    hub.attach(page)
    hub.mark_ready(page)

    async def go():
        await hub.state("thinking")
        for level in range(8):
            await hub.level(level / 10, 0.5)
        await hub.speak(FakeSpeech())
        await hub.level(0.9, 0.9)
        await hub.state("listening")
        await hub.drain()

    asyncio.run(go())
    assert page.got.count("speak") == 1 and page.got.count("state") == 2
    assert page.got.count("mic_level") <= 3                     # one in flight, then the newest
    assert page.got[-1] == "state"                              # order kept


def test_a_page_that_stops_reading_is_closed_so_it_reconnects(monkeypatch):
    monkeypatch.setattr(app, "OUTBOX_MAX", 5)

    class Stuck(Page):
        async def send_json(self, message):
            await asyncio.sleep(10)

    hub, page = app.Hub(), Stuck()
    hub.attach(page)

    async def go():
        for i in range(8):
            await hub.send({"type": "state", "state": "thinking", "turn": i})
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert page not in hub._clients and page.closed == 1011


# ---------------------------------------------------------------- over the route
def test_a_settings_write_over_the_socket_echoes_set_and_hint_never_the_value(tmp_path, monkeypatch):
    """ADR-022 at the boundary the page sees: the token goes in, `{set, hint}` comes back."""
    path = tmp_path / "settings.json"
    real_load = config.load
    monkeypatch.setattr(config, "load", lambda settings_file=None, env=None: real_load(settings_file or path, env={}))
    monkeypatch.setattr(config, "env_overrides", lambda environ=None: {})
    monkeypatch.setattr(app.settings_view, "device_options", lambda fresh=False: {"input": [], "output": []})
    hub = app.Hub()
    with connect(hub) as client, client.websocket_connect("/ws", headers=PAGE) as ws:
        first = ws.receive_json()
        assert first["type"] == "settings" and first["values"]["BUNPRO_API_TOKEN"] == {"set": False, "hint": ""}
        ws.send_json({"type": "settings", "values": {"BUNPRO_API_TOKEN": "bp-secret-value-1234", "SUBTITLES": "off"}})
        reply = ws.receive_json()
        assert reply["type"] == "settings" and reply["saved"] == ["BUNPRO_API_TOKEN", "SUBTITLES"]
        assert reply["values"]["BUNPRO_API_TOKEN"] == {"set": True, "hint": "…1234"}
        assert "bp-secret-value-1234" not in json.dumps(reply, ensure_ascii=False)
    assert "bp-secret-value-1234" in path.read_text(encoding="utf-8")


def test_an_explain_click_is_answered_on_the_socket_that_asked(masked):
    hub = app.Hub()
    asked = []

    async def explain(kind, text, context, lang):
        asked.append((kind, text, context, lang))
        return "A condition.", ""

    hub.explain = explain
    with connect(hub) as client:
        with client.websocket_connect("/ws", headers=PAGE) as a, client.websocket_connect("/ws", headers=PAGE) as b:
            a.receive_json(); b.receive_json()
            a.send_json({"type": "explain", "kind": "grammar", "text": "〜たら", "context": "雨が降ったら。", "lang": "ja"})
            answer = a.receive_json()
            assert answer["type"] == "explanation" and answer["answer"] == "A condition." and answer["text"] == "〜たら"
            assert asked == [("grammar", "〜たら", "雨が降ったら。", "ja")]
            b.send_json({"type": "control", "action": "ready"})
            b.send_json({"type": "shout"})                 # a round trip proves nothing else came
            assert b.receive_json()["type"] == "error"


def test_without_an_explainer_the_answer_says_so_rather_than_spinning(masked):
    hub = app.Hub()
    with connect(hub) as client, client.websocket_connect("/ws", headers=PAGE) as ws:
        ws.receive_json()
        ws.send_json({"type": "explain", "kind": "sentence", "text": "はい。"})
        reply = ws.receive_json()
        assert reply["type"] == "explanation" and reply["answer"] == "" and "not available" in reply["error"]


def test_two_pages_both_hear_her_but_only_started_ones_get_audio(masked):
    hub = app.Hub()
    with connect(hub) as client:
        with client.websocket_connect("/ws", headers=PAGE) as a, client.websocket_connect("/ws", headers=PAGE) as b:
            a.receive_json(); b.receive_json()
            a.send_json({"type": "control", "action": "ready"})
            a.send_json({"type": "shout"})                 # a round trip: `ready` has been handled
            assert a.receive_json()["type"] == "error"
            assert hub._ready == {list(hub._clients)[0]}   # a joined first
            client.portal.call(hub.state, "thinking")
            assert a.receive_json()["type"] == "state" and b.receive_json()["type"] == "state"
            client.portal.call(hub.speak, FakeSpeech())
            assert a.receive_json()["type"] == "speak"
            b.send_json({"type": "nonsense"})              # b gets its error and never a speak
            assert b.receive_json()["type"] == "error"


def test_a_late_page_is_told_the_state_she_is_in(masked):
    """A page that reconnects mid-turn must know she is speaking, or its first press is not
    treated as an interruption (src/main.ts `interrupt`)."""
    hub = app.Hub()
    asyncio.run(hub.state("thinking"))
    asyncio.run(hub.state("speaking"))
    with connect(hub) as client, client.websocket_connect("/ws", headers=PAGE) as ws:
        assert ws.receive_json()["type"] == "settings"
        state = ws.receive_json()
        assert state == {"type": "state", "state": "speaking", "turn": 1, "spoken": False}


def test_a_frame_that_is_not_json_is_answered_not_fatal(masked):
    hub = app.Hub()
    with connect(hub) as client, client.websocket_connect("/ws", headers=PAGE) as ws:
        ws.receive_json()
        ws.send_text("{not json")
        error = ws.receive_json()
        assert error["type"] == "error" and "JSON" in error["message"]
        ws.send_json({"type": "settings_test", "service": "voicevox"})   # a type that is gone
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "control", "action": "bargein_ack"})       # an action that is gone
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "control", "action": "ready"})             # still alive
        ws.send_json({"type": "shout"})
        assert ws.receive_json()["type"] == "error" and len(hub._ready) == 1


# ---------------------------------------------------------------- the listener
def test_a_port_already_in_use_is_one_clear_error_not_a_dead_url():
    taken = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    cfg = types.SimpleNamespace(HOST="127.0.0.1", PORT=port, STATUS_HEARTBEAT_S=30)
    try:
        with pytest.raises(RuntimeError, match=r"cannot listen on http://127\.0\.0\.1:\d+/.*PORT"):
            asyncio.run(app.serve(app.Hub(), cfg))
    finally:
        taken.close()


def test_a_free_port_is_bound_here_and_handed_to_uvicorn():
    sock = app.listen("127.0.0.1", 0)
    try:
        assert sock.getsockname()[0] == "127.0.0.1" and sock.getsockname()[1] > 0
    finally:
        sock.close()


def test_a_point_from_their_list_she_forgot_to_mark_goes_red_anyway():
    """「と思います」 stayed black although 「と思う」 was on the list (user, 2026-09-12): the hub
    adds what the annotator finds, after her own marks, and never for a point not on the list."""
    from types import SimpleNamespace
    hub = app.Hub()
    from backend.chunker import GrammarMark
    hub.study = SimpleNamespace(items=[SimpleNamespace(text="と思う", kind="grammar"),
                                      SimpleNamespace(text="雨", kind="vocab")])
    seen = []
    def points(text, names, marks):
        seen.append((text, names, marks))
        return [{"start": 6, "end": 11, "point": "と思う"}]
    hub.points = points
    text = "明日は雨が降ると思います。"
    speech = SimpleNamespace(grammar=(GrammarMark(3, 4, "〜が"),))
    assert hub._grammar(text, speech) == [{"start": 3, "end": 4, "point": "〜が", "level": ""},
                                          {"start": 6, "end": 11, "point": "と思う", "level": ""}]
    assert seen == [(text, ["と思う"], [{"start": 3, "end": 4, "point": "〜が", "level": ""}])]
    hub.points = lambda *_: 1 / 0                                  # a broken finder costs nothing
    assert hub._grammar(text, speech) == [{"start": 3, "end": 4, "point": "〜が", "level": ""}]


def test_each_grammar_mark_carries_the_bunpro_level_of_its_point():
    """The mark is painted the colour of its level (user, 2026-09-12): looked up on the student's
    own list by normalised name, containment allowed (〜たら is たら); a point not on the list has
    no level and keeps the neutral style. A leech among their words is flagged the same way."""
    from types import SimpleNamespace
    from backend.chunker import GrammarMark
    hub = app.Hub()
    hub.study = SimpleNamespace(
        items=[SimpleNamespace(text="たら", kind="grammar", srs="beginner"),
               SimpleNamespace(text="と思う", kind="grammar", srs="ghost"),
               SimpleNamespace(text="雨", kind="vocab", srs="", leech=True)],
        spans=lambda text: [{"start": 0, "end": 1, "word": "雨", "reading": "あめ", "meaning": "rain",
                             "stage": "Apprentice 2"}])
    text = "雨が降ったらと思うでしょう。"
    speech = SimpleNamespace(grammar=(GrammarMark(2, 6, "〜たら"), GrammarMark(6, 9, "と思う"),
                                      GrammarMark(9, 13, "でしょう")))
    assert [(m["point"], m["level"]) for m in hub._grammar(text, speech)] == [
        ("〜たら", "beginner"), ("と思う", "ghost"), ("でしょう", "")]
    assert hub._vocab(text) == [{"start": 0, "end": 1, "word": "雨", "reading": "あめ", "meaning": "rain",
                                 "stage": "Apprentice 2", "leech": True}]
    hub.study = None
    assert [m["level"] for m in hub._grammar(text, speech)] == ["", "", ""]


def test_a_page_that_joins_later_gets_the_lesson_so_far_without_the_audio(masked):
    """A reload sat on "she is thinking of how to start…" for an opening it had already heard
    (user, 2026-09-12): the welcome now says she has spoken and replays the transcript."""
    hub = app.Hub()
    asyncio.run(hub.state("thinking"))
    speech = FakeSpeech()
    speech.grammar = (types.SimpleNamespace(start=0, end=2, point="〜たら"),)
    asyncio.run(hub.speak(speech))
    asyncio.run(hub.bargein())                                     # cut short: the page may not have heard it all
    asyncio.run(hub.transcript("雨です", accepted=False, reason="blocklist"))
    asyncio.run(hub.state("listening"))
    with connect(hub) as client, client.websocket_connect("/ws", headers=PAGE) as ws:
        got = [ws.receive_json() for _ in range(3)]
    assert [m["type"] for m in got] == ["settings", "state", "history"]
    assert got[1]["spoken"] is True
    lines = got[2]["lines"]
    assert [(l["who"], l["text"]) for l in lines] == [("her", "はい。"), ("you", "雨です")]
    assert lines[0]["grammar"] == [{"start": 0, "end": 2, "point": "〜たら", "level": ""}]
    assert lines[0]["turn"] == 1 and lines[0]["cut"] is True
    assert lines[1]["accepted"] is False and lines[1]["reason"] == "blocklist"
    assert not any(k in lines[0] for k in ("audio_b64", "visemes", "vtimes", "vdurations"))


def test_a_fresh_lesson_replays_nothing_and_the_transcript_is_capped(masked):
    hub = app.Hub()
    with connect(hub) as client, client.websocket_connect("/ws", headers=PAGE) as ws:
        assert ws.receive_json()["type"] == "settings"
        ws.send_json({"type": "control", "action": "ready"})       # a round trip: nothing else was queued
        assert hub.history.maxlen == app.HISTORY_LINES
    for i in range(app.HISTORY_LINES + 5):
        asyncio.run(hub.transcript(str(i)))
    assert len(hub.history) == app.HISTORY_LINES and hub.history[0]["text"] == "5"
    assert hub.spoken is False


def test_her_mark_on_one_of_their_words_is_a_word_span_not_a_grammar_mark():
    """The tutor wraps the student's WaniKani words like grammar — {{申します|申す}} — so her marks
    are the authority for both (prompts/tutor.md, 2026-09-12): a mark naming a word on their list
    becomes the word's span, her mark wins over what the list found on its own, and nothing is
    marked twice."""
    from types import SimpleNamespace
    from backend.chunker import GrammarMark
    hub = app.Hub()
    text = "山田と申します。雨が降ったら帰ります。"
    hub.study = SimpleNamespace(
        items=[SimpleNamespace(text="申す", kind="vocab", reading="もうす", meaning="to be called", stage=2, leech=False, srs=""),
               SimpleNamespace(text="たら", kind="grammar", srs="adept")],
        # The list's own substring find overlaps her mark: hers wins, once.
        spans=lambda t: [{"start": 3, "end": 5, "word": "申す", "reading": "もうす", "meaning": "to be called", "stage": "Apprentice 2"}])
    speech = SimpleNamespace(grammar=(GrammarMark(3, 7, "申す"), GrammarMark(10, 14, "〜たら")))
    hub.grammar = lambda t, marks: [m for m in marks if m["point"] != "申す"]   # the guard never sees the word anyway
    assert hub._grammar(text, speech) == [{"start": 10, "end": 14, "point": "〜たら", "level": "adept"}]
    assert hub._vocab(text, speech) == [{"start": 3, "end": 7, "word": "申す", "reading": "もうす", "meaning": "to be called",
                                         "stage": "Apprentice 2", "leech": False}]
    assert hub._vocab(text) == [{"start": 3, "end": 5, "word": "申す", "reading": "もうす", "meaning": "to be called",
                                 "stage": "Apprentice 2", "leech": False}]      # the student's own line: no marks
    hub.study, hub.grammar = SimpleNamespace(items=[], spans=lambda t: []), None
    assert [m["point"] for m in hub._grammar(text, speech)] == ["申す", "〜たら"]    # not on their list: grammar, as before


def test_a_point_on_their_list_is_grammar_whatever_the_guard_thinks():
    from types import SimpleNamespace
    from backend.chunker import GrammarMark
    hub = app.Hub()
    hub.study = SimpleNamespace(items=[SimpleNamespace(text="つもり", kind="grammar", srs="beginner")], spans=lambda t: [])
    hub.grammar = lambda t, marks: []                                   # a guard that drops everything
    speech = SimpleNamespace(grammar=(GrammarMark(0, 3, "つもり"), GrammarMark(4, 6, "先生")))
    assert hub._grammar("つもりの先生", speech) == [{"start": 0, "end": 3, "point": "つもり", "level": "beginner"}]
