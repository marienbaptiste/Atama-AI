"""Service status registry (spec §5b, ADR-019): states, sanitisation, snapshot, dump, table."""
import json

from backend.status import SERVICES, StatusRegistry


def test_states_validated():
    r = StatusRegistry()
    r.report("wanikani", "ok", "level 12")
    try:
        r.report("wanikani", "connected")
    except ValueError:
        pass
    else:
        raise AssertionError


def test_unknown_service_rejected():
    r = StatusRegistry()
    try:
        r.report("claude", "ready")          # the chip is `brain` (ADR-027), not the provider's name
    except ValueError:
        pass
    else:
        raise AssertionError


def test_sanitizer_masks_registered_secret_and_header_echo():
    r = StatusRegistry()
    # Deliberately short fakes so `make check-secrets` does not flag this file.
    r.register_secret("tok-abc-12")
    st = r.report("bunpro", "error", last_error="401 for Token token=tok-abc-12; Bearer zzzzzzzz")
    assert "tok-abc" not in st.last_error
    assert "zzzzzzzz" not in st.last_error


def test_sanitizer_masks_bearer_and_token_echoes_without_registration():
    r = StatusRegistry()
    assert r.sanitize("Authorization: Bearer abcdefgh1234") == "Authorization: Bearer ***"
    assert r.sanitize("hdr='Token token=qwerty99', next") == "hdr='Token token=***', next"
    # Two echoes on one line, each masked; a quote or comma ends the value.
    out = r.sanitize("Bearer one111 and Bearer two222)")
    assert out == "Bearer *** and Bearer ***)"
    assert r.sanitize("") == "" and r.sanitize("nothing here") == "nothing here"


def test_sanitizer_applies_to_detail_too_and_ignores_short_secrets():
    r = StatusRegistry()
    r.register_secret("abc")                 # too short to be a secret; must not mask every "abc"
    r.register_secret("longer-secret")
    st = r.report("wanikani", "ok", detail="abc longer-secret")
    assert st.detail == "abc ***"


def test_listener_fires_on_change_only():
    r = StatusRegistry()
    seen = []
    r.subscribe(lambda s: seen.append(s.state))
    r.report("voicevox", "ok", "0.20")
    r.report("voicevox", "ok", "0.20")
    r.report("voicevox", "down")
    assert seen == ["ok", "down"]


def test_snapshot_is_the_ws_message_per_service():
    r = StatusRegistry()
    r.report("voicevox", "warm", "0.25.2, 5 styles")
    r.report("stt", "error", "load failed", last_error="CUDA out of memory")
    snap = r.snapshot()
    assert set(snap) == {"voicevox", "stt"}
    assert snap["voicevox"]["type"] == "service_status" and snap["voicevox"]["state"] == "warm"
    assert snap["stt"]["last_error"] == "CUDA out of memory" and snap["stt"]["updated_at"] > 0
    assert r.get("stt").state == "error" and r.get("brain") is None
    snap["voicevox"]["state"] = "down"      # a copy: the registry does not change underneath
    assert r.get("voicevox").state == "warm"


def test_dump_persists_the_sanitised_snapshot(tmp_path):
    r = StatusRegistry()
    r.register_secret("tok-abc-12")
    r.report("bunpro", "error", "x", last_error="Bearer tok-abc-12")
    path = tmp_path / "srs" / "status.json"          # parent does not exist yet
    r.dump(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["bunpro"]["last_error"] == "Bearer ***"
    assert "tok-abc-12" not in path.read_text(encoding="utf-8")


def test_table_lists_every_service_in_spec_order_with_errors_in_brackets():
    r = StatusRegistry()
    r.report("wanikani", "ok", "level 4 (synced 09:12)")
    r.report("bunpro", "stale", "N4", last_error="timed out")
    rows = r.table().splitlines()
    assert rows[0].startswith("service") and len(rows) == 1 + len(SERVICES)
    assert [row.split()[0] for row in rows[1:]] == list(SERVICES)
    assert rows[1] == "wanikani     ok             level 4 (synced 09:12)"
    assert rows[2] == "bunpro       stale          N4  [timed out]"
    assert "(no report)" in rows[3]
