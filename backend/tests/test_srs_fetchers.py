"""Bunpro + WaniKani fetchers against sanitised golden fixtures (pinned live 2026-09-09)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend import constants
from backend.srs import bunpro as bp
from backend.srs import wanikani as wk
from backend.srs.http import SrsClient

FX = Path(__file__).parent / "fixtures"


def fx(service: str, name: str):
    return json.loads((FX / service / f"{name}.json").read_text(encoding="utf-8"))


# ----------------------------------------------------------------- Bunpro
def bunpro_raw():
    return {n: fx("bunpro", n) for n in ("user", "due", "jlpt_progress", "srs_overview", "ghost_grammar",
                                         "srs_level_beginner_grammar", "forecast_daily")}


def test_bunpro_parse_golden():
    p = bp.parse(bunpro_raw())
    assert p.username == "student"
    assert set(p.jlpt_grammar) == {"N5", "N4", "N3", "N2", "N1"}
    assert p.jlpt_grammar["N4"]["total"] == 185
    assert p.jlpt_grammar["N4"]["learned"] == 12 + 38 + 10
    assert p.current_jlpt() == "N4"
    assert p.srs_grammar["ghost"] == 1
    assert len(p.ghosts) == 1 and p.ghosts[0].title == "そういう" and p.ghosts[0].jlpt == "N4" and p.ghosts[0].srs == "ghost"
    assert 1 <= len(p.weak_grammar) <= 15 and all(g.srs == "beginner" for g in p.weak_grammar)
    assert p.forecast_tomorrow_grammar == 15


def test_bunpro_parse_degrades_on_missing_and_garbage():
    p = bp.parse({})
    assert p.jlpt_grammar == {} and p.ghosts == [] and p.due_grammar == 0
    p = bp.parse({"due": "nonsense", "jlpt_progress": {"grammar": {"4": "x"}}, "ghost_grammar": {"reviews": {"data": [{"attributes": {"reviewable_id": 1}}]}}})
    assert p.ghosts == []


def test_bunpro_fetch_raw_uses_only_get_and_named_levels():
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, dict(req.url.params)))
        name = req.url.path.rsplit("/", 1)[-1]
        mapping = {"user": "user", "due": "due", "jlpt_progress_mixed": "jlpt_progress", "srs_level_overview": "srs_overview",
                   "srs_ghost_level_details": "ghost_grammar", "srs_level_details": "srs_level_beginner_grammar", "forecast_daily": "forecast_daily"}
        return httpx.Response(200, json=fx("bunpro", mapping[name]))

    c = SrsClient("bunpro", "tok-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    raw = bp.fetch_raw(c)
    assert raw["_errors"] == {}
    assert all(m == "GET" for m, _, _ in calls)
    lvl = next(p for _, path, p in calls if path.endswith("srs_level_details"))
    assert lvl["level"] == "beginner" and lvl["reviewable_type"] == "Grammar"
    assert all(p[constants.BUNPRO_TOKEN_OPT_IN_PARAM] == "true" for _, _, p in calls)


def test_bunpro_one_endpoint_failing_does_not_kill_profile():
    def handler(req):
        if req.url.path.endswith("srs_level_details"):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=fx("bunpro", "due") if req.url.path.endswith("/due") else fx("bunpro", "jlpt_progress"))

    c = SrsClient("bunpro", "tok-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    prof, errors = bp.fetch(c)
    assert "srs_level_beginner_grammar" in errors and "500" in errors["srs_level_beginner_grammar"]
    assert prof.weak_grammar == []


# ----------------------------------------------------------------- WaniKani
def wanikani_raw():
    return {"user": fx("wanikani", "user"), "summary": fx("wanikani", "summary"), "assignments": fx("wanikani", "assignments_vocab"),
            "review_statistics": fx("wanikani", "review_statistics_vocab"), "subjects": fx("wanikani", "subjects_recent")}


def test_wanikani_parse_golden():
    p = wk.parse(wanikani_raw())
    assert p.level == 4 and p.subscription_active is True and p.username == "student"
    assert sum(p.stage_counts.values()) == len(fx("wanikani", "assignments_vocab")["data"])
    assert 1 <= len(p.recent_unlocks) <= 30
    v = p.recent_unlocks[0]
    assert v.characters and v.reading and v.meaning and 1 <= v.level <= 4
    # recent_unlocks are ordered newest first
    assert p.leeches == []  # fixture has zero low-accuracy items


def test_wanikani_leech_derivation():
    raw = wanikani_raw()
    sid = raw["subjects"]["data"][0]["id"]
    raw["review_statistics"] = {"data": [{"data": {"subject_id": sid, "meaning_incorrect": 5, "reading_incorrect": 3, "percentage_correct": 60}}]}
    p = wk.parse(raw)
    assert len(p.leeches) == 1 and p.leeches[0].incorrect == 8


def test_wanikani_fetch_raw_paginates_and_requests_subjects():
    calls = []
    page1 = fx("wanikani", "assignments_vocab")
    page1 = {**page1, "pages": {"per_page": 500, "next_url": f"{constants.WANIKANI_ORIGIN}/v2/assignments?page_after_id=999&subject_types=vocabulary"}}
    page2 = {"data": [], "pages": {"next_url": None}}

    def handler(req):
        calls.append((req.method, req.url.path, dict(req.url.params)))
        if req.url.path == "/v2/user":
            return httpx.Response(200, json=fx("wanikani", "user"))
        if req.url.path == "/v2/summary":
            return httpx.Response(200, json=fx("wanikani", "summary"))
        if req.url.path == "/v2/assignments":
            return httpx.Response(200, json=page2 if "page_after_id" in req.url.params else page1)
        if req.url.path == "/v2/review_statistics":
            return httpx.Response(200, json=fx("wanikani", "review_statistics_vocab"))
        if req.url.path == "/v2/subjects":
            return httpx.Response(200, json=fx("wanikani", "subjects_recent"))
        return httpx.Response(404)

    c = SrsClient("wanikani", "tok-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    raw = wk.fetch_raw(c)
    assert raw["_errors"] == {}
    assert all(m == "GET" for m, _, _ in calls)
    # Two collections are read: the student's vocabulary, and (for the chat's furigana, spec §8b)
    # their kanji. Each follows its next page; the follow-up carries the cursor from `next_url`.
    first_pages = [p for _, path, p in calls if path == "/v2/assignments" and "page_after_id" not in p]
    assert [p.get("subject_types") for p in first_pages] == ["vocabulary", "kanji"]
    assert sum(1 for _, path, p in calls if path == "/v2/assignments" and "page_after_id" in p) == 2
    subj = next(p for _, path, p in calls if path == "/v2/subjects" and "ids" in p)
    assert len(subj["ids"].split(",")) <= 30 + 30
    kanji = next(p for _, path, p in calls if path == "/v2/subjects" and p.get("types") == "kanji")
    assert kanji["levels"].startswith("1")        # levels 1..the student's, never every level


def test_wanikani_user_payload_has_no_permissions_field():
    """V0.9 finding: token scopes are not exposed by /v2/user; the doctor cannot verify read-only."""
    assert "permissions" not in fx("wanikani", "user")["data"]
