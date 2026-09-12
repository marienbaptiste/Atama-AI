"""Config resolution, the dead .env, secret masking (ADR-022)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from backend import config

REPO = Path(__file__).resolve().parents[2]


def test_every_setting_is_presentable_in_the_panel():
    """config.py is the only inventory now that .env.example is gone (ADR-022 amendment): every
    key needs the group and description the settings page renders it from."""
    for s in config.SCHEMA:
        assert s.group and s.description, f"{s.key} would appear in the panel unexplained"
    assert len(set(config.KEYS)) == len(config.KEYS)


def test_a_leftover_dotenv_no_longer_configures_anything(tmp_path, monkeypatch):
    """The trap this removed: a key in .env that the settings page could not change."""
    env = tmp_path / ".env"
    env.write_text("PORT=9999\nWANIKANI_TOKEN=abc123\n", encoding="utf-8")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    cfg = config.load(tmp_path / "s.json")
    assert cfg.PORT != 9999 and not str(cfg.WANIKANI_TOKEN)
    assert config.stale_dotenv() == ["PORT", "WANIKANI_TOKEN"]          # reported, not read


def test_the_bootstrap_key_in_a_dotenv_is_not_reported_as_stale(tmp_path, monkeypatch):
    """SETTINGS_FILE says where settings.json is, so it never moved into it."""
    (tmp_path / ".env").write_text("SETTINGS_FILE=settings.json\n", encoding="utf-8")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    assert config.stale_dotenv() == []


def test_resolution_order(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"port": 9001, "claude_model": "opus"}), encoding="utf-8")
    cfg = config.load(settings, env={"PORT": "9002"})
    assert cfg.PORT == 9002            # env wins
    assert cfg.CLAUDE_MODEL == "opus"  # settings.json wins over default
    assert cfg.VAD_SILENCE_MS == 900   # default
    assert cfg.first_run is False


def test_process_env_is_read_only_under_the_atama_prefix(monkeypatch):
    """Our key names collide with Claude Code's own exports; bare env vars must not win."""
    monkeypatch.setenv("CLAUDE_EFFORT", "max")        # what Claude Code sets in its shell
    monkeypatch.setenv("ATAMA_CLAUDE_EFFORT", "low")  # what we would set deliberately
    monkeypatch.setenv("ATAMA_NOT_A_SETTING", "x")
    assert config.env_overrides() == {"CLAUDE_EFFORT": "low"}


def test_bare_claude_env_var_does_not_leak_into_config(tmp_path):
    monkeypatch_free = config.load(tmp_path / "s.json", env={})
    assert monkeypatch_free.CLAUDE_EFFORT == "medium"


def test_dotenv_parser(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\nWANIKANI_TOKEN=abc123  # trailing\nBUNPRO_API_TOKEN="quoted=value"\nEMPTY=\nbad line\n', encoding="utf-8")
    assert config.read_dotenv(p) == {"WANIKANI_TOKEN": "abc123", "BUNPRO_API_TOKEN": "quoted=value", "EMPTY": ""}
    assert config.read_dotenv(tmp_path / "missing") == {}


def test_claude_cwd_default_is_outside_repo_and_stable(tmp_path):
    cfg = config.load(tmp_path / "s.json", env={})
    p = config.claude_cwd(cfg)
    assert config.REPO_ROOT not in p.parents and p != config.REPO_ROOT
    assert p == config.claude_cwd(cfg)  # stable: --resume relies on the same cwd
    cfg2 = config.load(tmp_path / "s.json", env={"CLAUDE_CWD": str(tmp_path / "x")})
    assert config.claude_cwd(cfg2) == (tmp_path / "x")
    # an in-repo cwd is refused (it would expose CLAUDE.md to the tutor) and falls back to the default
    import warnings
    cfg3 = config.load(tmp_path / "s.json", env={"CLAUDE_CWD": ".cache/claude-cwd"})
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        assert config.claude_cwd(cfg3) == p
    assert any("inside the repository" in str(x.message) for x in w)


def test_first_run_when_no_settings_file(tmp_path):
    cfg = config.load(tmp_path / "nope.json", env={})
    assert cfg.first_run is True


def test_save_is_atomic_and_merges(tmp_path):
    settings = tmp_path / "settings.json"
    config.save({"BUNPRO_API_TOKEN": "abcdefgh12345678"}, settings)
    config.save({"PORT": 8123}, settings)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data == {"bunpro_api_token": "abcdefgh12345678", "port": 8123}
    assert not list(tmp_path.glob(".settings-*"))  # temp file cleaned up


def test_public_view_masks_secrets(tmp_path):
    settings = tmp_path / "settings.json"
    config.save({"WANIKANI_TOKEN": "wk-token-value-9999"}, settings)
    view = config.load(settings, env={}).public_view()
    assert view["WANIKANI_TOKEN"] == {"set": True, "hint": "…9999"}
    assert view["BUNPRO_API_TOKEN"] == {"set": False, "hint": ""}
    assert "wk-token-value" not in json.dumps(view)


def test_type_coercion_error_is_clear(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"port": "not-a-number"}), encoding="utf-8")
    try:
        config.load(settings, env={})
    except ValueError as e:
        assert "PORT" in str(e)
    else:
        raise AssertionError("expected ValueError")


# ------------------------------------------------------------ validation (2026-09-12)
def test_a_value_outside_its_choices_is_refused_by_name(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"turn_mode": "telepathy"}), encoding="utf-8")
    with __import__("pytest").raises(config.ConfigError) as exc:
        config.load(settings, env={})
    msg = str(exc.value)
    assert "TURN_MODE" in msg and "ptt, vad" in msg and str(settings) in msg
    assert "\n" not in msg                                   # one line, not a traceback's worth


def test_a_number_outside_its_range_is_refused_by_name(tmp_path):
    import pytest
    with pytest.raises(config.ConfigError, match="PORT.*at most 65535"):
        config.load(tmp_path / "s.json", env={"PORT": "70000"})
    with pytest.raises(config.ConfigError, match="VAD_SILENCE_MS.*at least 0"):
        config.load(tmp_path / "s.json", env={"VAD_SILENCE_MS": "-5"})
    with pytest.raises(config.ConfigError, match="CONTEXT_ROTATE_AT.*at most 1"):
        config.load(tmp_path / "s.json", env={"CONTEXT_ROTATE_AT": "1.5"})
    assert config.load(tmp_path / "s.json", env={"PORT": "65535", "VAD_SILENCE_MS": "0"}).PORT == 65535


def test_malformed_settings_json_is_one_clear_line(tmp_path):
    import pytest
    settings = tmp_path / "settings.json"
    settings.write_text("{not json", encoding="utf-8")
    with pytest.raises(config.ConfigError) as exc:
        config.load(settings, env={})
    assert str(settings) in str(exc.value) and "\n" not in str(exc.value)
    settings.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="JSON object"):
        config.load(settings, env={})
    assert issubclass(config.ConfigError, ValueError)        # callers catching ValueError still do


def test_choices_are_declared_on_the_schema_and_shared_with_the_panel():
    from backend import settings_view
    assert settings_view.CHOICES is config.CHOICES
    assert config.CHOICES["TURN_MODE"] == ("ptt", "vad")
    assert config.CHOICES["CLAUDE_EFFORT"] == ("low", "medium", "high", "xhigh", "max")
    for s in config.SCHEMA:
        if s.choices:
            assert s.type is str and s.default in s.choices, s.key
        if s.low is not None or s.high is not None:
            assert s.type in (int, float), s.key
            assert (s.low is None or s.default >= s.low) and (s.high is None or s.default <= s.high), s.key


def test_the_filler_knob_is_gone_until_fillers_exist():
    """spec §10 / ADR-008 describe a filler pool that is not built; a knob for it did nothing."""
    assert "FILLER_AFTER_MS" not in config.KEYS


def test_the_tunables_that_left_the_modules_keep_their_values():
    """spec §11: the defaults moved into the schema; the rationale stays on the module constants."""
    from backend import stt, vad, voice_loop
    defaults = {s.key: s.default for s in config.SCHEMA}
    assert defaults["STT_QUIET_RMS"] == stt.QUIET_RMS
    assert defaults["STT_MIN_AVG_LOGPROB"] == stt.MIN_AVG_LOGPROB
    assert defaults["STT_MAX_NO_SPEECH_PROB"] == stt.MAX_NO_SPEECH_PROB
    assert defaults["STT_CORROBORATING_AVG_LOGPROB"] == stt.CORROBORATING_AVG_LOGPROB
    assert defaults["VAD_SPEECH_THRESHOLD"] == vad.SPEECH_THRESHOLD
    assert defaults["VAD_ONSET_TOLERANCE_MS"] == vad.ONSET_TOLERANCE_MS
    assert defaults["QUIET_OVER_FLOOR"] == voice_loop.QUIET_OVER_FLOOR
    assert defaults["PTT_HANDOVER_TIMEOUT_S"] == voice_loop.PTT_HANDOVER_TIMEOUT_S
    assert defaults["VOICEVOX_TIMEOUT_S"] == 30.0
