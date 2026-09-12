"""Spec §11 redaction test: a session written with fake tokens configured leaves no configured
secret value — and no `Bearer …` / `Token token=…` header echo — anywhere under logs/ or .cache/.

The leak paths the spec names are error strings and tool-call args, so those are what this
session is made of: an SRS fetch whose server echoes the Authorization header back in a 401, a
brain error quoting the environment, a tool error quoting the header, all routed through the
status registry (the logging boundary) and then into every file the app persists.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from backend import config
from backend.memory import Memory
from backend.srs import profile as profile_api
from backend.status import StatusRegistry

# Long enough to register with the sanitiser, not shaped like any real token.
WK = "wk-fake-secret-value-0123456789"
BP = "bp-fake-secret-value-9876543210"


def files_under(*roots: Path) -> list[Path]:
    return [p for root in roots if root.exists() for p in root.rglob("*") if p.is_file()]


def test_no_configured_secret_reaches_logs_or_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)          # LOG_DIR / CACHE_DIR resolve under here
    cfg = config.load(tmp_path / "settings.json", env={"WANIKANI_TOKEN": WK, "BUNPRO_API_TOKEN": BP})
    secrets = list(cfg.secrets().values())
    assert secrets == [WK, BP]
    log_dir, cache_dir = cfg.path("LOG_DIR"), cfg.path("CACHE_DIR")
    registry = StatusRegistry()
    for value in secrets:
        registry.register_secret(value)

    # 1. SRS snapshot + status.json: both servers reject with the header echoed in the body.
    def echo_header(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": f"rejected {req.headers['Authorization']}"})

    overrides = {svc: {"transport": httpx.MockTransport(echo_header), "sleep": lambda s: None}
                 for svc in ("wanikani", "bunpro")}
    profile = profile_api.build(WK, BP, cache_dir / "srs", 0, 5, registry, client_overrides=overrides)
    assert profile.sources == {"wanikani": "error", "bunpro": "error"}
    assert (cache_dir / "srs" / "status.json").is_file()

    # 2. Brain and tool errors through the registry, re-persisted.
    registry.report("brain", "error", "spawn failed",
                    last_error=f"child env WANIKANI_TOKEN={WK}; header Authorization: Bearer {WK}")
    registry.report("bunpro_mcp", "failed", registry.sanitize(f"HTTP 401 for Token token={BP}"))
    registry.dump(cache_dir / "srs" / "status.json")

    # 3. The turn log (the Anki mine) with a tool error, sanitised at the boundary.
    mem = Memory(root=cache_dir / "memory", sessions=log_dir / "sessions", session_id="redaction")
    mem.record_turn(student="こんにちは", tutor_sentences=[{"text": "はい。", "emotion": "happy"}],
                    tools=[{"name": "get_ghost_reviews", "args": {}, "ok": False,
                            "error": registry.sanitize(f"bunpro: HTTP 401 for /user: Token token={BP}")}])
    assert mem.log_path().is_file()

    # 4. The profile dump and a session header with the resolved config + status table.
    profile_api.debug_snapshot(profile, profile_api.render(profile), log_dir, "2026-09-12")
    (log_dir / "session-2026-09-12.jsonl").write_text(
        json.dumps({"type": "header", "config": cfg.public_view(), "status": registry.snapshot(),
                    "table": registry.table()}, ensure_ascii=False) + "\n", encoding="utf-8")

    written = files_under(log_dir, cache_dir)
    assert len(written) >= 4, written
    for path in written:
        text = path.read_text(encoding="utf-8")
        for value in secrets:
            assert value not in text, f"{value[:8]}… leaked into {path.relative_to(tmp_path)}"
        assert "Bearer " + WK not in text and "Token token=" + BP not in text
        assert f"Bearer {WK[:6]}" not in text


def test_status_snapshot_masks_header_echoes_even_for_unregistered_values(tmp_path):
    """The belt-and-braces path: a header echo of a value nobody registered still never lands."""
    registry = StatusRegistry()
    registry.report("wanikani", "error", "x", last_error="401: Authorization: Bearer unregistered9val")
    registry.dump(tmp_path / "status.json")
    text = (tmp_path / "status.json").read_text(encoding="utf-8")
    assert "unregistered9val" not in text and "Bearer ***" in text
