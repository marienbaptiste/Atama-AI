"""profile.build(): per-service budget and the snapshot fallback on timeout (spec §5b).

Neither path had a test, which is how a timeout came to throw away a perfectly good
snapshot and report `error` — the state the spec reserves for "no cache" (2026-09-09).
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

from backend.srs import profile as profile_api
from backend.srs.cache import cache_store
from backend.status import StatusRegistry

FX = Path(__file__).parent / "fixtures"


def bunpro_raw():
    names = ("user", "due", "jlpt_progress", "srs_overview", "ghost_grammar",
             "srs_level_beginner_grammar", "forecast_daily")
    return {n: json.loads((FX / "bunpro" / f"{n}.json").read_text(encoding="utf-8")) for n in names}


def snapshot(path: Path, raw: dict, when: str = "2026-09-09T09:00:00+00:00") -> None:
    cache_store(path, {"fetched_at": when, "service": "bunpro", "raw": raw})


def slow_one(delays: dict[str, float]):
    """Stand-in for _one that just takes time, so the test is about build()'s scheduling."""
    def _fake(service, token, cache_path, min_age_s, registry, deadline, client_kwargs=None, force=False):
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

    def never(service, token, cache_path, min_age_s, registry, deadline, client_kwargs=None, force=False):
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
    def never(service, token, cache_path, min_age_s, registry, deadline, client_kwargs=None, force=False):
        time.sleep(1.0)
        return object(), "ok", {}

    monkeypatch.setattr(profile_api, "_one", never)
    reg = StatusRegistry()
    prof = profile_api.build("", "bp-token", tmp_path, 0.0, 0.1, reg)

    assert prof.sources["bunpro"] == "error"
    assert prof.bunpro is None
    assert "no snapshot" in reg.snapshot()["bunpro"]["detail"]
