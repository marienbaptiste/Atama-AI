"""The WebSocket contract (spec §8, ROADMAP subsystem 7, gate M3a): defined once in models.py,
mirrored into the page by generation, and every message round-trips.

Drift fails here: a message added, renamed or given a new field on the server changes the rendered
TypeScript, and the committed frontend/src/protocol.gen.ts no longer matches it.
"""
from __future__ import annotations

import re

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from backend import app, config, models
from backend.tools import gen_protocol

#: One valid instance of every server message. A new type must be added here to be tested, and the
#: first test below fails until it is.
SERVER_SAMPLES = {
    "state": {"state": "thinking", "turn": 3},
    "stt_partial": {"text": "こん"},
    "stt_final": {"text": "雨です", "accepted": False, "reason": "blocklist",
                  "readings": [{"start": 0, "end": 1, "reading": "あめ", "known": True}]},
    "assistant_text": {"text": "はい。"},
    "speak": {"audio_b64": "UklGRg==", "visemes": ["aa", "sil"], "vtimes": [100.0, 180.0],
              "vdurations": [80.0, 50.0], "text": "あ。", "emotion": "happy", "turn": 2,
              "grammar": [{"start": 0, "end": 1, "point": "〜たら"}], "target": "〜たら", "used": "雨",
              "readings": [{"start": 0, "end": 1, "reading": "あ", "known": False}]},
    "emotion": {"emotion": "thinking"},
    "bargein": {"turn": 2},
    "srs_profile": {"text": "WaniKani level 4"},
    "service_status": {"service": "brain", "state": "ready", "detail": "claude-cli", "last_error": ""},
    "settings": {"values": {"TURN_MODE": "ptt"}, "fields": [{"key": "TURN_MODE"}], "pinned": {},
                 "saved": ["TURN_MODE"], "errors": {}},
    "mic_level": {"level": 0.012, "speech": 0.9},
    "meters": {"context_tokens": 6204, "context_window": 200000, "month_turns": 12},
    "timing": {"stt_ms": 400.0, "first_audio_ms": 3100.0, "p90_ms": 4800.0, "turns": 9},
    "explanation": {"kind": "grammar", "text": "〜たら", "answer": "A condition.", "error": ""},
    "error": {"message": "unrecognised message", "fatal": False},
}

CLIENT_SAMPLES = {
    "audio_chunk": {"pcm16_b64": "AAAA", "seq": 1},
    "control": {"action": "new_topic"},
    "settings": {"values": {"SUBTITLES": "off"}},
    "settings_test": {"service": "voicevox"},
    "explain": {"kind": "grammar", "text": "〜たら", "context": "雨が降ったら。", "lang": "ja"},
}


def test_every_message_type_has_a_sample():
    assert set(SERVER_SAMPLES) == models.SERVER_TYPES
    assert set(CLIENT_SAMPLES) == models.CLIENT_TYPES


def test_the_page_types_are_generated_from_the_models_and_current():
    assert gen_protocol.OUT.exists(), "run python -m backend.tools.gen_protocol"
    committed = gen_protocol.OUT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == gen_protocol.render(), (
        "frontend/src/protocol.gen.ts is stale - run python -m backend.tools.gen_protocol")


def test_the_generated_types_name_exactly_the_model_types():
    text = gen_protocol.render()
    listed = lambda name: set(re.findall(r'"([a-z_]+)"', re.search(  # noqa: E731
        rf"export const {name} = \[(.*?)\]", text).group(1)))
    assert listed("CLIENT_TYPES") == models.CLIENT_TYPES
    assert listed("SERVER_TYPES") == models.SERVER_TYPES
    assert listed("RESERVED_TYPES") == models.RESERVED_TYPES


def test_every_field_reaches_the_page():
    text = gen_protocol.render()
    for union in (models.ClientMessage, models.ServerMessage):
        for model in gen_protocol._members(union):
            block = re.search(rf"export interface {model.__name__}Msg \{{(.*?)\n\}}", text, re.S)
            assert block, model.__name__
            for name in model.model_fields:
                assert re.search(rf"^\s+{name}\??:", block.group(1), re.M), f"{model.__name__}.{name}"


@pytest.mark.parametrize("kind", sorted(SERVER_SAMPLES))
def test_every_server_message_round_trips(kind):
    message = models.ServerMessageAdapter.validate_python({"type": kind, **SERVER_SAMPLES[kind]})
    dumped = message.model_dump()
    assert dumped["type"] == kind
    assert models.ServerMessageAdapter.validate_python(dumped).model_dump() == dumped


@pytest.mark.parametrize("kind", sorted(CLIENT_SAMPLES))
def test_every_client_message_parses(kind):
    message = models.ClientMessageAdapter.validate_python({"type": kind, **CLIENT_SAMPLES[kind]})
    assert message.type == kind


def test_a_misspelt_field_is_rejected_not_ignored():
    with pytest.raises(ValidationError):
        models.ClientMessageAdapter.validate_python({"type": "control", "action": "start", "turn": 1})


def test_an_unknown_client_message_is_answered_with_an_error_and_the_socket_lives(monkeypatch):
    monkeypatch.setattr(app.settings_view, "snapshot",
                        lambda *a, **k: {"values": {}, "fields": [], "pinned": {}})
    hub = app.Hub()
    with TestClient(app.build(hub)) as client, client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "settings"
        ws.send_json({"type": "shout", "text": "!"})
        error = ws.receive_json()
        assert error["type"] == "error" and "unrecognised" in error["message"]
        ws.send_json({"type": "nonsense"})
        assert ws.receive_json()["type"] == "error"          # still connected, still answering


def test_the_reserved_partial_is_never_emitted():
    """stt_partial exists so streaming STT is not a protocol change - and is never sent (spec §8)."""
    backend = config.REPO_ROOT / "backend"
    skip = {backend / "models.py", backend / "tools" / "gen_protocol.py"}
    for path in backend.rglob("*.py"):
        if path in skip or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert "SttPartial" not in text and "stt_partial" not in text, path


def test_every_emotion_the_voice_knows_the_face_knows():
    """ADR-020: a tag reaches the voice AND the face. The face's table is frontend/src/rig.ts."""
    from backend import emotions
    rig = (config.REPO_ROOT / "frontend" / "src" / "rig.ts").read_text(encoding="utf-8")
    table = rig.split("export const RIG", 1)[1].split("\n};", 1)[0]
    faces = set(re.findall(r"^  (\w+):\s+\{", table, re.M))
    voice = {tag for tag in emotions.DEFAULT_TABLE if tag}      # "" is neutral: rigFor("") handles it
    assert voice and voice <= faces, voice - faces
    assert "neutral" in faces
