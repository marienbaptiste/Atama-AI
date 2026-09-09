"""Profile renderer: ≤ 600 tokens, zero/one/both sources, session-start orchestration + status."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from backend.srs import bunpro as bp
from backend.srs import profile as pf
from backend.srs import wanikani as wk
from backend.srs.cache import cache_store
from backend.status import StatusRegistry

FX = Path(__file__).parent / "fixtures"


def fx(service, name):
    return json.loads((FX / service / f"{name}.json").read_text(encoding="utf-8"))


def full_profile():
    w = wk.parse({"user": fx("wanikani", "user"), "summary": fx("wanikani", "summary"), "assignments": fx("wanikani", "assignments_vocab"),
                  "review_statistics": fx("wanikani", "review_statistics_vocab"), "subjects": fx("wanikani", "subjects_recent")})
    b = bp.parse({n: fx("bunpro", n) for n in ("user", "due", "jlpt_progress", "srs_overview", "ghost_grammar", "srs_level_beginner_grammar", "forecast_daily")})
    # inflate lists to worst case
    w.recent_unlocks = (w.recent_unlocks * 6)[:30]
    w.leeches = (w.recent_unlocks * 2)[:15]
    b.ghosts = (b.weak_grammar * 4)[:15]
    b.weak_grammar = (b.weak_grammar * 4)[:15]
    return pf.StudentProfile(wanikani=w, bunpro=b, sources={"wanikani": "ok", "bunpro": "ok"})


def test_render_full_within_600_tokens():
    text = pf.render(full_profile())
    assert pf.estimate_tokens(text) <= pf.MAX_TOKENS, text
    assert "WaniKani level 4" in text and "N4" in text and "そういう" in text


def test_render_zero_one_both():
    none = pf.render(pf.StudentProfile())
    assert "not configured" in none and pf.estimate_tokens(none) <= pf.MAX_TOKENS
    only_b = pf.render(pf.StudentProfile(bunpro=full_profile().bunpro))
    assert "WaniKani: not connected" in only_b and "Bunpro grammar" in only_b
    only_w = pf.render(pf.StudentProfile(wanikani=full_profile().wanikani))
    assert "Bunpro: not connected" in only_w and "Recent WaniKani vocab" in only_w


def test_estimate_tokens_is_conservative():
    assert pf.estimate_tokens("そういう") == 4
    assert pf.estimate_tokens("abcd" * 10) == 10


def bunpro_handler(req):
    name = req.url.path.rsplit("/", 1)[-1]
    mapping = {"user": "user", "due": "due", "jlpt_progress_mixed": "jlpt_progress", "srs_level_overview": "srs_overview",
               "srs_ghost_level_details": "ghost_grammar", "srs_level_details": "srs_level_beginner_grammar", "forecast_daily": "forecast_daily"}
    return httpx.Response(200, json=fx("bunpro", mapping[name]))


def test_build_disabled_ok_and_stale_states(tmp_path):
    reg = StatusRegistry()
    # zero tokens -> both disabled
    p = pf.build("", "", tmp_path, 3600, 5, reg)
    assert p.sources == {"wanikani": "disabled", "bunpro": "disabled"}
    # bunpro live ok (wanikani disabled)
    p = pf.build("", "tok-b", tmp_path, 3600, 5, reg, client_overrides={"bunpro": {"transport": httpx.MockTransport(bunpro_handler), "sleep": lambda s: None}})
    assert p.sources["bunpro"] == "ok" and reg.get("bunpro").state == "ok" and (tmp_path / "bunpro.json").exists()
    # bunpro network dead but cache present -> stale
    dead = httpx.MockTransport(lambda r: httpx.Response(503, text="down"))
    p = pf.build("", "tok-b", tmp_path, 0, 5, reg, client_overrides={"bunpro": {"transport": dead, "sleep": lambda s: None}})  # ttl 0 => cache not fresh
    assert p.sources["bunpro"] == "stale" and reg.get("bunpro").state == "stale"
    # no cache, dead -> error
    p = pf.build("", "tok-b", tmp_path / "empty", 3600, 5, reg, client_overrides={"bunpro": {"transport": dead, "sleep": lambda s: None}})
    assert p.sources["bunpro"] == "error" and reg.get("bunpro").state == "error"


def test_status_detail_never_contains_token(tmp_path):
    reg = StatusRegistry()
    tok = "tok-secret-abcdef"
    reg.register_secret(tok)
    dead = httpx.MockTransport(lambda r: httpx.Response(401, text=f"denied {tok}"))
    pf.build("", tok, tmp_path, 3600, 5, reg, client_overrides={"bunpro": {"transport": dead, "sleep": lambda s: None}})
    st = reg.get("bunpro")
    assert tok not in st.last_error and tok not in st.detail


def test_fresh_cache_short_circuits_network(tmp_path):
    reg = StatusRegistry()
    raw = {n: fx("bunpro", n) for n in ("user", "due", "jlpt_progress", "srs_overview", "ghost_grammar", "srs_level_beginner_grammar", "forecast_daily")}
    cache_store(tmp_path / "bunpro.json", {"fetched_at": "2026-09-09T00:00:00+00:00", "service": "bunpro", "raw": raw})
    hits = []
    p = pf.build("", "tok-b", tmp_path, 3600, 5, reg, client_overrides={"bunpro": {"transport": httpx.MockTransport(lambda r: hits.append(1) or httpx.Response(500)), "sleep": lambda s: None}})
    assert p.sources["bunpro"] == "ok" and hits == []
    assert "synced 0" in reg.get("bunpro").detail or "synced" in reg.get("bunpro").detail


def test_manual_refresh_forces_fetch_even_when_fresh(tmp_path):
    reg = StatusRegistry()
    raw = {n: fx("bunpro", n) for n in ("user", "due", "jlpt_progress", "ghost_grammar", "srs_level_beginner_grammar", "forecast_daily")}
    cache_store(tmp_path / "bunpro.json", {"fetched_at": "2026-09-09T00:00:00+00:00", "service": "bunpro", "raw": raw})
    hits = []

    def h(r):
        hits.append(r.url.path)
        return bunpro_handler(r)

    p = pf.build("", "tok-b", tmp_path, 3600, 5, reg, client_overrides={"bunpro": {"transport": httpx.MockTransport(h), "sleep": lambda s: None}}, force=True)
    assert p.sources["bunpro"] == "ok" and len(hits) == 6  # exactly the six launch/refresh calls, once
    assert (tmp_path / "status.json").exists()
