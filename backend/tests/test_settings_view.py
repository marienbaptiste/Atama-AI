"""Settings panel backend (spec §11, ADR-022). Hermetic: a temp settings.json, a fake environment.

`.env` is read from the real repo root by config.py, so every test here passes an explicit
`environ` and stubs the dotenv read — a developer's own .env must not decide a test's outcome.
"""
from __future__ import annotations

import json

import pytest

from backend import config, models, settings_view as sv


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    monkeypatch.setattr(config, "read_dotenv", lambda path: {})


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    real_load = config.load
    monkeypatch.setattr(config, "load", lambda settings_file=None, env=None:
                        real_load(settings_file or path, env={}))
    return path


def test_schema_covers_every_key_once():
    assert [s["key"] for s in sv.schema()] == list(config.KEYS)


def test_a_stored_secret_never_reaches_the_echo(store):
    store.write_text(json.dumps({"wanikani_token": "wk-secret-abcdef1234"}), encoding="utf-8")
    echo = sv.snapshot(config.load(), environ={})
    assert echo["values"]["WANIKANI_TOKEN"] == {"set": True, "hint": "…1234"}
    assert "wk-secret-abcdef1234" not in json.dumps(echo, ensure_ascii=False)


def test_the_echo_fits_the_protocol_model(store):
    models.Settings(**sv.apply({"VOICEVOX_SPEED_SCALE": "1.1"}, environ={}))


def test_a_blank_secret_leaves_the_stored_one_alone(store):
    sv.apply({"WANIKANI_TOKEN": "wk-secret-abcdef1234"}, environ={})
    echo = sv.apply({"WANIKANI_TOKEN": ""}, environ={})
    assert echo["values"]["WANIKANI_TOKEN"]["set"] is True
    assert json.loads(store.read_text(encoding="utf-8"))["wanikani_token"] == "wk-secret-abcdef1234"


def test_values_are_coerced_and_persisted(store):
    echo = sv.apply({"VOICEVOX_SPEED_SCALE": "1.1", "MEMORY_ENABLED": False, "VAD_SILENCE_MS": "800"},
                    environ={})
    assert echo["saved"] == ["MEMORY_ENABLED", "VAD_SILENCE_MS", "VOICEVOX_SPEED_SCALE"]
    assert echo["values"]["VOICEVOX_SPEED_SCALE"] == 1.1 and echo["values"]["VAD_SILENCE_MS"] == 800
    assert echo["values"]["MEMORY_ENABLED"] is False and echo["errors"] == {}


def test_bad_input_is_a_per_key_error_and_saves_the_rest(store):
    echo = sv.apply({"VAD_SILENCE_MS": "soon", "TURN_MODE": "telepathy", "NOPE": 1,
                     "SUBTITLES": "off"}, environ={})
    assert set(echo["errors"]) == {"VAD_SILENCE_MS", "TURN_MODE", "NOPE"}
    assert echo["saved"] == ["SUBTITLES"]


def test_host_can_never_be_changed_from_the_page(store):
    """A browser must not be able to rebind the server off loopback (ADR-017)."""
    echo = sv.apply({"HOST": "0.0.0.0"}, environ={})
    assert "HOST" in echo["errors"] and echo["values"]["HOST"] == "127.0.0.1"


def test_a_key_pinned_by_the_environment_is_refused_not_silently_lost(store, monkeypatch):
    monkeypatch.setattr(config, "read_dotenv", lambda path: {"CLAUDE_MODEL": "opus", "PORT": ""})
    env = {"ATAMA_TURN_MODE": "vad"}
    assert sv.pinned(env) == {"CLAUDE_MODEL": ".env", "TURN_MODE": "environment"}
    echo = sv.apply({"CLAUDE_MODEL": "haiku", "PORT": 8001}, environ=env)
    assert "CLAUDE_MODEL" in echo["errors"] and echo["saved"] == ["PORT"]


def test_a_masked_echo_sent_back_is_rejected_not_stored(store):
    echo = sv.apply({"BUNPRO_API_TOKEN": {"set": True, "hint": "…abcd"}}, environ={})
    assert "BUNPRO_API_TOKEN" in echo["errors"] and not store.exists()


def test_the_socket_opens_with_a_masked_echo_and_saves_through_it(store, monkeypatch):
    """End to end over the real WebSocket route: what the page actually receives."""
    from starlette.testclient import TestClient
    from backend import app

    monkeypatch.setattr(config, "env_overrides", lambda environ=None: {})
    store.write_text(json.dumps({"wanikani_token": "wk-secret-abcdef1234"}), encoding="utf-8")
    with TestClient(app.build(app.Hub())) as client, client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "settings" and len(first["fields"]) == len(config.KEYS)
        assert "wk-secret-abcdef1234" not in json.dumps(first, ensure_ascii=False)
        ws.send_json({"type": "settings", "values": {"SUBTITLES": "off", "HOST": "0.0.0.0"}})
        reply = ws.receive_json()
        assert reply["saved"] == ["SUBTITLES"] and "HOST" in reply["errors"]
        assert reply["values"]["SUBTITLES"] == "off"


def test_device_pickers_offer_what_is_connected_once_each(store, monkeypatch):
    from backend import audio
    devices = [
        audio.Device(0, "Microsoft Sound Mapper - Input", "input", 2, 44100, hostapi="MME"),
        audio.Device(1, "Microphone (Chat-Audeze Maxwel", "input", 1, 48000, hostapi="MME"),
        audio.Device(5, "Microphone (Chat-Audeze Maxwell)", "input", 1, 48000, hostapi="Windows DirectSound"),
        audio.Device(6, "Microphone (Realtek HD Audio Mic input)", "input", 2, 44100,
                     hostapi="Windows WDM-KS", usable=False),
        audio.Device(2, "Primary Sound Driver", "output", 2, 44100, hostapi="Windows DirectSound"),
        audio.Device(3, "Speakers (Realtek(R) Audio)", "output", 2, 44100, hostapi="MME"),
    ]
    monkeypatch.setattr(audio, "list_devices", lambda kind=None: devices)
    monkeypatch.setattr(sv, "latest", None)
    fields = {f["key"]: f for f in sv.snapshot(config.load(), environ={})["fields"]}
    assert fields["AUDIO_INPUT_DEVICE"]["options"] == ["Microphone (Chat-Audeze Maxwell)"]
    assert fields["AUDIO_OUTPUT_DEVICE"]["options"] == ["Speakers (Realtek(R) Audio)"]
    assert fields["CLAUDE_MODEL"]["options"] is None
    assert fields["AUDIO_INPUT_DEVICE"]["live"] and not fields["CLAUDE_MODEL"]["live"]


def test_the_watchers_list_wins_over_this_processes_stale_one(store, monkeypatch):
    monkeypatch.setattr(sv, "latest", {"input": ["Plugged In Just Now"], "output": []})
    fields = {f["key"]: f for f in sv.snapshot(config.load(), environ={})["fields"]}
    assert fields["AUDIO_INPUT_DEVICE"]["options"] == ["Plugged In Just Now"]


def test_a_live_key_save_reaches_the_running_session(store, monkeypatch):
    from starlette.testclient import TestClient
    from backend import app

    monkeypatch.setattr(config, "env_overrides", lambda environ=None: {})
    hub = app.Hub()
    applied = []
    hub.on_settings = applied.append
    with TestClient(app.build(hub)) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "settings", "values": {"AUDIO_INPUT_DEVICE": "Headset"}})
        ws.receive_json()
    assert applied == [["AUDIO_INPUT_DEVICE"]]


def test_a_page_that_connects_late_still_hears_the_last_mic_status(store, monkeypatch):
    import asyncio
    from starlette.testclient import TestClient
    from backend import app

    monkeypatch.setattr(config, "env_overrides", lambda environ=None: {})
    hub = app.Hub()
    asyncio.run(hub.status("microphone", "missing", "no microphone found"))
    with TestClient(app.build(hub)) as client, client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "settings"
        status = ws.receive_json()
        assert status["type"] == "service_status" and status["state"] == "missing"


def test_mic_level_is_part_of_the_protocol_and_only_sent_to_watchers():
    import asyncio
    from backend import app

    assert "mic_level" in models.SERVER_TYPES
    hub, sent = app.Hub(), []

    async def capture(message):
        sent.append(message)

    hub.send = capture
    asyncio.run(hub.level(0.01, 0.9))
    assert sent == []                                   # no page open: nothing built or sent
    hub._clients.add(object())
    asyncio.run(hub.level(0.0123456, 0.9))
    assert sent == [{"type": "mic_level", "level": 0.01235, "speech": 0.9}]
