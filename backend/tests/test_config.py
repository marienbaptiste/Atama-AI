"""Config resolution, inventory sync with .env.example, secret masking (ADR-022)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from backend import config

REPO = Path(__file__).resolve().parents[2]


def test_env_example_lists_exactly_the_schema_keys():
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    keys = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, flags=re.M))
    assert keys == set(config.KEYS), f"missing in .env.example: {set(config.KEYS) - keys}; extra: {keys - set(config.KEYS)}"


def test_resolution_order(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"port": 9001, "claude_model": "opus"}), encoding="utf-8")
    cfg = config.load(settings, env={"PORT": "9002"})
    assert cfg.PORT == 9002            # env wins
    assert cfg.CLAUDE_MODEL == "opus"  # settings.json wins over default
    assert cfg.VAD_SILENCE_MS == 600   # default
    assert cfg.first_run is False


def test_process_env_is_read_only_under_the_atama_prefix(monkeypatch):
    """Our key names collide with Claude Code's own exports; bare env vars must not win."""
    monkeypatch.setenv("CLAUDE_EFFORT", "max")        # what Claude Code sets in its shell
    monkeypatch.setenv("ATAMA_CLAUDE_EFFORT", "low")  # what we would set deliberately
    monkeypatch.setenv("ATAMA_NOT_A_SETTING", "x")
    assert config.env_overrides() == {"CLAUDE_EFFORT": "low"}


def test_bare_claude_env_var_does_not_leak_into_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_EFFORT", "max")
    monkeypatch.setattr(config, "read_dotenv", lambda p: {})
    assert config.load(tmp_path / "s.json").CLAUDE_EFFORT == "high"


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
