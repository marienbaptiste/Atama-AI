"""`.env` -> `settings.json` migration: nothing secret moves, nothing effective changes."""
from __future__ import annotations

import json

import pytest

from backend import config
from backend.tools import migrate_env

ENV = """# a comment that must survive
WANIKANI_TOKEN=wk-secret-abcdef1234
CLAUDE_MODEL=opus
VOICEVOX_SPEED_SCALE=0.95
MEMORY_ENABLED=false
EMOTION_HAPPY=
SETTINGS_FILE=settings.json

# ---- docker compose only ----
SEARXNG_SECRET=docker-secret
"""


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "env_overrides", lambda environ=None: {})
    env = tmp_path / ".env"
    env.write_text(ENV, encoding="utf-8")
    return env, tmp_path / "settings.json"


def test_dry_run_reports_and_changes_nothing(files):
    env, settings = files
    report = migrate_env.migrate(env, settings, dry_run=True)
    assert report["moved"] == ["CLAUDE_MODEL", "MEMORY_ENABLED", "VOICEVOX_SPEED_SCALE"]
    assert report["dropped"] == ["EMOTION_HAPPY"] and report["invalid"] == []
    assert env.read_text(encoding="utf-8") == ENV and not settings.exists()


def test_public_keys_move_and_everything_else_stays(files):
    env, settings = files
    report = migrate_env.migrate(env, settings)
    stored = json.loads(settings.read_text(encoding="utf-8"))
    assert stored == {"claude_model": "opus", "voicevox_speed_scale": 0.95, "memory_enabled": False}
    left = env.read_text(encoding="utf-8")
    for kept in ("WANIKANI_TOKEN=", "SEARXNG_SECRET=", "SETTINGS_FILE=",
                 "# a comment that must survive"):
        assert kept in left
    for gone in ("CLAUDE_MODEL=", "VOICEVOX_SPEED_SCALE=", "MEMORY_ENABLED=", "EMOTION_HAPPY="):
        assert gone not in left
    assert (env.parent / report["backup"].split("\\")[-1].split("/")[-1]).read_text(encoding="utf-8") == ENV


def test_a_secret_never_reaches_settings_json(files):
    env, settings = files
    migrate_env.migrate(env, settings)
    assert "wk-secret" not in settings.read_text(encoding="utf-8")


def test_the_effective_configuration_is_unchanged(files):
    env, settings = files
    before = migrate_env._effective(settings, env)
    migrate_env.migrate(env, settings)
    assert migrate_env._effective(settings, env) == before


def test_a_failure_puts_both_files_back(files, monkeypatch):
    env, settings = files
    settings.write_text('{"subtitles": "off"}', encoding="utf-8")
    real = migrate_env._effective
    calls = []

    def lying(s, e):
        calls.append(1)
        out = real(s, e)
        if len(calls) > 1:
            out["CLAUDE_MODEL"] = "something else"
        return out

    monkeypatch.setattr(migrate_env, "_effective", lying)
    with pytest.raises(RuntimeError):
        migrate_env.migrate(env, settings)
    assert env.read_text(encoding="utf-8") == ENV
    assert settings.read_text(encoding="utf-8") == '{"subtitles": "off"}'


def test_an_unparseable_value_is_reported_and_left_where_it_is(tmp_path):
    """Moving it would turn a bad .env line into a bad settings.json entry; the user fixes it."""
    env = tmp_path / ".env"
    env.write_text("VAD_SILENCE_MS=soon\nCLAUDE_MODEL=opus\n", encoding="utf-8")
    move, drop, invalid = migrate_env.plan(env)
    assert move == {"CLAUDE_MODEL": "opus"} and invalid == ["VAD_SILENCE_MS"] and drop == []


def test_nothing_to_do_makes_no_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "env_overrides", lambda environ=None: {})
    env = tmp_path / ".env"
    env.write_text("WANIKANI_TOKEN=x\n", encoding="utf-8")
    assert migrate_env.migrate(env, tmp_path / "settings.json")["backup"] is None
    assert list(tmp_path.glob(".env.bak-*")) == []
