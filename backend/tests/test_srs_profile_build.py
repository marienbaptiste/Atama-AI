"""profile.build(): per-service budget and the snapshot fallback on timeout (spec §5b).

Neither path had a test, which is how a timeout came to throw away a perfectly good
snapshot and report `error` — the state the spec reserves for "no cache" (2026-09-09).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path

import httpx

from backend.srs import profile as profile_api
from backend.srs.cache import cache_store
from backend.srs.http import ReadOnlyViolation
from backend.status import StatusRegistry

FX = Path(__file__).parent / "fixtures"


def bunpro_raw():
    names = ("user", "due", "jlpt_progress", "ghost_grammar",
             "srs_level_beginner_grammar", "forecast_daily")
    return {n: json.loads((FX / "bunpro" / f"{n}.json").read_text(encoding="utf-8")) for n in names}


def snapshot(path: Path, raw: dict, when: str = "2026-09-09T09:00:00+00:00") -> None:
    cache_store(path, {"fetched_at": when, "service": "bunpro", "raw": raw})


def slow_one(delays: dict[str, float]):
    """Stand-in for _one that just takes time, so the test is about build()'s scheduling."""
    def _fake(service, token, cache_path, min_age_s, registry, deadline, client_kwargs=None, force=False, cancel=None):
        time.sleep(delays[service])
        registry.report(service, "ok", f"{service} fake")
        return object(), "ok", {}
    return _fake


def test_each_service_gets_its_own_budget_not_a_shared_pot(tmp_path, monkeypatch):
    """Futures are collected in order; a slow WaniKani must not eat Bunpro's budget.

    wanikani 0.40s then bunpro 0.70s, budget 0.50s: under one shared deadline bunpro is left
    0.10s and times out. Per service, it gets its own 0.50s from when we start waiting on it.
    """
    monkeypatch.setattr(profile_api, "_one", slow_one({"wanikani": 0.40, "bunpro": 0.70}))
    reg = StatusRegistry()
    prof = profile_api.build("wk-token", "bp-token", tmp_path, 0.0, 0.5, reg)
    assert prof.sources == {"wanikani": "ok", "bunpro": "ok"}, prof.sources


def test_a_timed_out_fetch_serves_the_last_snapshot_as_stale(tmp_path, monkeypatch):
    """spec §5b: `stale` is "serving cache; last fetch failed" — the data must survive."""
    snapshot(tmp_path / "bunpro.json", bunpro_raw())

    def never(service, token, cache_path, min_age_s, registry, deadline, client_kwargs=None, force=False, cancel=None):
        time.sleep(1.0)
        return object(), "ok", {}

    monkeypatch.setattr(profile_api, "_one", never)
    reg = StatusRegistry()
    prof = profile_api.build("", "bp-token", tmp_path, 0.0, 0.1, reg)

    assert prof.sources["bunpro"] == "stale"
    assert prof.bunpro is not None and prof.bunpro.current_jlpt() == "N4"
    chip = reg.snapshot()["bunpro"]
    # _hhmm renders the snapshot time in LOCAL time, so derive it rather than hard-coding.
    local = dt.datetime.fromisoformat("2026-09-09T09:00:00+00:00").astimezone().strftime("%H:%M")
    assert chip["state"] == "stale"
    assert "timed out" in chip["detail"] and f"snapshot {local}" in chip["detail"]


def test_a_timed_out_fetch_without_a_snapshot_is_an_error(tmp_path, monkeypatch):
    """`error` stays reserved for the case the spec gives it: no cache to fall back on."""
    def never(service, token, cache_path, min_age_s, registry, deadline, client_kwargs=None, force=False, cancel=None):
        time.sleep(1.0)
        return object(), "ok", {}

    monkeypatch.setattr(profile_api, "_one", never)
    reg = StatusRegistry()
    prof = profile_api.build("", "bp-token", tmp_path, 0.0, 0.1, reg)

    assert prof.sources["bunpro"] == "error"
    assert prof.bunpro is None
    assert "no snapshot" in reg.snapshot()["bunpro"]["detail"]


# ----------------------------------------------------------------- partial / 429 / violation / cancel
_MAP = {"user": "user", "due": "due", "jlpt_progress_mixed": "jlpt_progress", "srs_ghost_level_details": "ghost_grammar",
        "srs_level_details": "srs_level_beginner_grammar", "forecast_daily": "forecast_daily"}


def bunpro_handler(fail: dict[str, httpx.Response] | None = None, calls: list | None = None):
    def handler(req):
        name = req.url.path.rsplit("/", 1)[-1]
        if calls is not None:
            calls.append(name)
        if fail and name in fail:
            return fail[name]
        return httpx.Response(200, json=json.loads((FX / "bunpro" / f"{_MAP[name]}.json").read_text(encoding="utf-8")))
    return handler


def overrides(handler):
    return {"bunpro": {"transport": httpx.MockTransport(handler), "sleep": lambda s: None}}


def test_a_partial_fetch_is_ok_with_its_failures_visible_and_is_fetched_again_next_launch(tmp_path):
    """Core endpoints ok, a secondary one failed: the student gets fresh data now and the chip
    says what is missing - and the snapshot is not re-served as complete until the TTL runs out.
    The next launch fetches again (no retry inside the session, ADR-024)."""
    reg = StatusRegistry()
    prof = profile_api.build("", "bp-token", tmp_path, 3600, 5, reg,
                             overrides(bunpro_handler({"forecast_daily": httpx.Response(500, text="boom")})))
    assert prof.sources["bunpro"] == "ok" and prof.bunpro.current_jlpt() == "N4"
    chip = reg.snapshot()["bunpro"]
    assert chip["state"] == "ok" and "partial: 1 endpoint(s) failed" in chip["detail"] and "500" in chip["last_error"]
    assert json.loads((tmp_path / "bunpro.json").read_text(encoding="utf-8"))["partial"] is True

    calls: list = []
    prof = profile_api.build("", "bp-token", tmp_path, 3600, 5, reg, overrides(bunpro_handler(calls=calls)))
    assert calls, "a partial snapshot must not short-circuit the next launch"
    assert prof.sources["bunpro"] == "ok" and reg.snapshot()["bunpro"]["last_error"] == ""
    assert json.loads((tmp_path / "bunpro.json").read_text(encoding="utf-8"))["partial"] is False

    calls.clear()
    profile_api.build("", "bp-token", tmp_path, 3600, 5, reg, overrides(bunpro_handler(calls=calls)))
    assert calls == [], "a complete, fresh snapshot is re-used"


def test_a_rate_limited_fetch_is_stale_with_the_reason_and_stops_calling(tmp_path):
    reg = StatusRegistry()
    calls: list = []
    handler = bunpro_handler({"srs_ghost_level_details": httpx.Response(429, headers={"Retry-After": "60"})}, calls)
    prof = profile_api.build("", "bp-token", tmp_path, 3600, 5, reg, overrides(handler))
    assert prof.sources["bunpro"] == "stale" and prof.bunpro is not None and prof.bunpro.current_jlpt() == "N4"
    chip = reg.snapshot()["bunpro"]
    assert chip["state"] == "stale" and "rate limited" in chip["detail"]
    assert "429" in chip["last_error"] and "Retry-After 60" in chip["last_error"] and "no retry" in chip["last_error"]
    assert calls == ["user", "due", "jlpt_progress_mixed", "srs_ghost_level_details"], calls

    # 429 on the very first call and no snapshot: `error`, and it says why.
    reg2 = StatusRegistry()
    prof = profile_api.build("", "bp-token", tmp_path / "empty", 3600, 5, reg2,
                             overrides(bunpro_handler({"user": httpx.Response(429)})))
    assert prof.sources["bunpro"] == "error" and reg2.snapshot()["bunpro"]["detail"] == "rate limited, no snapshot"


def test_a_read_only_violation_reaches_the_chip_as_error_and_is_logged_critical(tmp_path, caplog):
    """spec §0: `raises ReadOnlyViolation ... logged as CRITICAL, status chip -> error`."""
    snapshot(tmp_path / "bunpro.json", bunpro_raw())   # even with a good snapshot: a violation is not `stale`

    def handler(req):
        raise ReadOnlyViolation("refused POST api.bunpro.jp/api/frontend/reviews")

    reg = StatusRegistry()
    with caplog.at_level(logging.CRITICAL, logger="backend.srs"):
        prof = profile_api.build("", "bp-token", tmp_path, 0, 5, reg, overrides(handler))
    assert prof.sources["bunpro"] == "error" and prof.bunpro is None
    chip = reg.snapshot()["bunpro"]
    assert chip["state"] == "error" and "violation" in chip["detail"] and "ReadOnlyViolation" in chip["last_error"]
    assert prof.errors["bunpro"]["violation"].startswith("ReadOnlyViolation")
    assert any(r.levelno == logging.CRITICAL and "GOLDEN RULE" in r.getMessage() for r in caplog.records)


def test_a_timed_out_worker_stops_at_its_next_request_and_stores_nothing(tmp_path):
    """The budget ran out: the worker is told to stop, its client refuses the next request, and
    no snapshot appears behind build()'s back to race a manual Refresh."""
    calls: list = []

    def handler(req):
        calls.append(req.url.path)
        time.sleep(0.5)
        return httpx.Response(200, json=json.loads((FX / "bunpro" / "user.json").read_text(encoding="utf-8")))

    reg = StatusRegistry()
    prof = profile_api.build("", "bp-token", tmp_path, 0, 0.1, reg, overrides(handler))
    assert prof.sources["bunpro"] == "error" and "timed out" in reg.snapshot()["bunpro"]["detail"]
    time.sleep(1.5)   # long enough for a runaway worker to have made every remaining call
    assert len(calls) == 1, calls
    assert not (tmp_path / "bunpro.json").exists()


def test_a_corrupt_snapshot_is_an_error_not_a_crash(tmp_path):
    path = tmp_path / "bunpro.json"
    path.write_text("{not json", encoding="utf-8")
    reg = StatusRegistry()
    prof, state, errors = profile_api._stale_or_error("bunpro", path, reg, 1.0)
    assert (prof, state) == (None, "error") and "no snapshot" in reg.snapshot()["bunpro"]["detail"]

    dead = httpx.MockTransport(lambda r: httpx.Response(503, text="down"))
    prof, state, errors = profile_api._one("bunpro", "bp-token", path, 3600, reg, time.monotonic() + 5,
                                           {"transport": dead, "sleep": lambda s: None})
    assert (prof, state) == (None, "error") and "503" in reg.snapshot()["bunpro"]["last_error"]
