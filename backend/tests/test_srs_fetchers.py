"""Bunpro + WaniKani fetchers against sanitised golden fixtures (pinned live 2026-09-09)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend import constants
from backend.srs import bunpro as bp
from backend.srs import wanikani as wk
from backend.srs.http import ReadOnlyViolation, SrsClient

FX = Path(__file__).parent / "fixtures"


def fx(service: str, name: str):
    return json.loads((FX / service / f"{name}.json").read_text(encoding="utf-8"))


# ----------------------------------------------------------------- Bunpro
def bunpro_raw():
    return {n: fx("bunpro", n) for n in ("user", "due", "jlpt_progress", "ghost_grammar",
                                         "srs_level_beginner_grammar", "forecast_daily")}


def test_bunpro_parse_golden():
    p = bp.parse(bunpro_raw())
    assert p.username == "student"
    assert set(p.jlpt_grammar) == {"N5", "N4", "N3", "N2", "N1"}
    assert p.jlpt_grammar["N4"]["total"] == 185
    assert p.jlpt_grammar["N4"]["learned"] == 12 + 38 + 10
    assert p.current_jlpt() == "N4"
    assert not hasattr(p, "srs_grammar")   # srs_level_overview is never fetched; the field is gone with it
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
        mapping = {"user": "user", "due": "due", "jlpt_progress_mixed": "jlpt_progress",
                   "srs_ghost_level_details": "ghost_grammar", "srs_level_details": "srs_level_beginner_grammar", "forecast_daily": "forecast_daily"}
        return httpx.Response(200, json=fx("bunpro", mapping[name]))

    c = SrsClient("bunpro", "tok-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    raw = bp.fetch_raw(c)
    assert raw["_errors"] == {}
    assert "srs_overview" not in raw and not any(p.endswith("srs_level_overview") for _, p, _ in calls)
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


def test_bunpro_429_stops_the_fetch_without_retry():
    """ADR-024: no aggressive retry after a 429. The first 429 ends the fetch; nothing else is sent."""
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if len(calls) == 2:
            return httpx.Response(429, headers={"Retry-After": "30"}, text="slow down")
        return httpx.Response(200, json={})

    c = SrsClient("bunpro", "tok-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    raw = bp.fetch_raw(c)
    assert len(calls) == 2, calls
    assert c.rate_limited and "429" in c.rate_limited and "Retry-After 30" in c.rate_limited
    assert "user" in raw and "due" not in raw
    assert all("429" in msg for msg in raw["_errors"].values()) and len(raw["_errors"]) == 7


def test_bunpro_read_only_violation_is_never_swallowed():
    """spec §0: a violation raised under the fetch propagates out of it, whatever else fails."""
    def handler(req):
        raise ReadOnlyViolation("refused POST api.bunpro.jp/x")

    c = SrsClient("bunpro", "tok-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(ReadOnlyViolation):
        bp.fetch_raw(c)


# ----------------------------------------------------------------- WaniKani pagination
def _paged(pages_by_cursor, path="/v2/assignments"):
    """Handler for one paginated collection; every other endpoint answers an empty payload."""
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, dict(req.url.params), str(req.url)))
        if req.url.path == path:
            return httpx.Response(200, json=pages_by_cursor[req.url.params.get("page_after_id")])
        return httpx.Response(200, json={"data": {"level": 2}} if req.url.path == "/v2/user" else {"data": []})
    return handler, calls


def test_next_url_cursor_is_taken_and_our_params_re_sent_without_double_encoding():
    """WaniKani's next_url percent-encodes commas; re-parsing it by hand and letting httpx encode
    again sent `levels=1%252C2`. Only the cursor comes from next_url; the filters are ours."""
    nxt = f"{constants.WANIKANI_ORIGIN}/v2/subjects?types=kanji&levels=1%2C2&page_after_id=4321"
    handler, calls = _paged({None: {"data": [{"id": 1}], "total_count": 2, "pages": {"per_page": 1000, "next_url": nxt}},
                             "4321": {"data": [{"id": 2}], "pages": {"next_url": None}}}, path="/v2/subjects")
    c = SrsClient("wanikani", "tok-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    merged = wk._page_all(c, "/v2/subjects", {"types": "kanji", "levels": "1,2"})
    assert [x["id"] for x in merged["data"]] == [1, 2] and "truncated_after_pages" not in merged
    second = calls[1]
    assert second[2] == {"types": "kanji", "levels": "1,2", "page_after_id": "4321"}
    assert "%252C" not in second[3] and second[1] == "/v2/subjects"


def test_page_cap_covers_a_level_60_student_and_truncation_is_reported():
    assert wk.MAX_PAGES * 500 >= 9_300          # every assignment WaniKani has, at 500 per page
    pages = {None: {"data": [{"id": 0}], "total_count": 99, "pages": {"next_url": f"{constants.WANIKANI_ORIGIN}/v2/assignments?page_after_id=1"}}}
    for i in range(1, 60):
        pages[str(i)] = {"data": [{"id": i}], "pages": {"next_url": f"{constants.WANIKANI_ORIGIN}/v2/assignments?page_after_id={i + 1}"}}
    handler, calls = _paged(pages)
    c = SrsClient("wanikani", "tok-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    merged = wk._page_all(c, "/v2/assignments", {"subject_types": "vocabulary"}, max_pages=3)
    assert len(merged["data"]) == 3 and merged["truncated_after_pages"] == 3
    assert sum(1 for _, path, _, _ in calls if path == "/v2/assignments") == 3

    # Through fetch_raw the truncation lands in `_warnings` (visible in the chip), not `_errors`
    # (which would flag the snapshot partial and re-fetch it every launch for nothing).
    handler, calls = _paged(pages)
    c = SrsClient("wanikani", "tok-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    old = wk.MAX_PAGES
    wk.MAX_PAGES = 2
    try:
        raw = wk.fetch_raw(c)
    finally:
        wk.MAX_PAGES = old
    assert raw["_errors"] == {}
    assert "truncated at 2 pages" in raw["_warnings"]["assignments"] and "of 99" in raw["_warnings"]["assignments"]


def test_wanikani_429_stops_the_fetch_and_violation_propagates():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(429, text="limit")

    c = SrsClient("wanikani", "tok-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    raw = wk.fetch_raw(c)
    assert calls == ["/v2/user"] and "user" not in raw and all("429" in e for e in raw["_errors"].values())

    c = SrsClient("wanikani", "tok-test", transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(ReadOnlyViolation("refused"))), sleep=lambda s: None)
    with pytest.raises(ReadOnlyViolation):
        wk.fetch_raw(c)
