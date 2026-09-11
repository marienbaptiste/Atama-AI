"""WaniKani fetcher — READ-ONLY (spec §0/§5, ADR-021). Official API v2; shapes pinned live 2026-09-09.

Endpoints (all GET through SrsClient, header Wanikani-Revision 20170710):
  /v2/user                 -> data.{level, username, subscription.{active,type,max_level_granted}, started_at}
                              NOTE (V0.9): token permissions are NOT exposed here; read-only scope can only be
                              guaranteed at token creation. The doctor tells the user; it cannot verify.
  /v2/summary              -> data.{reviews: [{available_at, subject_ids}], lessons: [...], next_reviews_at}
  /v2/assignments?subject_types=vocabulary&started=true
                           -> data[].data.{subject_id, subject_type, srs_stage 1..9, unlocked_at, started_at,
                              passed_at, burned_at, available_at, hidden}; pages.{per_page: 500, next_url}
  /v2/review_statistics?subject_types=vocabulary&percentages_less_than=80
                           -> data[].data.{subject_id, meaning_incorrect, reading_incorrect, percentage_correct, ...}
  /v2/subjects?ids=1,2,3   -> data[].{object: "vocabulary", data.{characters, level, slug, meanings[{meaning,primary}],
                              readings[{reading,primary}], parts_of_speech}}
  /v2/assignments?subject_types=kanji&started=true    (the chat's furigana, spec §8b)
                           -> data[].data.{subject_id, srs_stage, passed_at, ...}
  /v2/subjects?types=kanji&levels=1,..,L               -> data[].{id, data.characters}
SRS stages: 1-4 apprentice, 5-6 guru, 7 master, 8 enlightened, 9 burned.

Kanji "known" = passed: `passed_at` is "Timestamp when the user reaches SRS stage 5 for the first
time" (API docs, revision 20170710, read 2026-09-12). The assignments endpoint has NO `passed`
filter (its filters: available_after/before, burned, hidden, ids, immediately_available_for_*,
in_review, levels, srs_stages, started, subject_ids, subject_types, unlocked, updated_after), and
`srs_stages=5..9` would drop a kanji that fell back below Guru — so started kanji are read and
filtered on `passed_at` here. Subject pages hold 1,000, assignment pages 500; 60 requests/minute.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from backend.srs.http import SrsClient

STAGE_BUCKETS = {1: "apprentice", 2: "apprentice", 3: "apprentice", 4: "apprentice",
                 5: "guru", 6: "guru", 7: "master", 8: "enlightened", 9: "burned"}
RECENT_LIMIT = 30
LEECH_LIMIT = 15


@dataclass
class Vocab:
    characters: str
    reading: str
    meaning: str
    level: int
    srs_stage: int
    incorrect: int = 0


@dataclass
class WaniKaniProfile:
    username: str = ""
    level: int = 0
    subscription_active: bool = False
    reviews_available_now: int = 0
    stage_counts: dict[str, int] = field(default_factory=dict)   # apprentice/guru/master/enlightened/burned
    recent_unlocks: list[Vocab] = field(default_factory=list)
    leeches: list[Vocab] = field(default_factory=list)
    #: Kanji passed (Guru reached at least once) — the chat hides their furigana. Not in the prompt.
    known_kanji: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ parsers
def _subject_index(subjects_payload: dict) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for item in subjects_payload.get("data") or []:
        d = item.get("data") or {}
        sid = item.get("id")
        if sid is None or not d.get("characters"):
            continue
        meaning = next((m["meaning"] for m in d.get("meanings") or [] if m.get("primary")), "")
        reading = next((r["reading"] for r in d.get("readings") or [] if r.get("primary")), "")
        out[int(sid)] = {"characters": d["characters"], "reading": reading, "meaning": meaning, "level": int(d.get("level") or 0)}
    return out


def parse(raw: dict[str, Any]) -> WaniKaniProfile:
    p = WaniKaniProfile()
    u = (raw.get("user") or {}).get("data") or {}
    p.username = str(u.get("username") or "")
    p.level = int(u.get("level") or 0)
    p.subscription_active = bool((u.get("subscription") or {}).get("active"))
    s = (raw.get("summary") or {}).get("data") or {}
    reviews = s.get("reviews") or []
    p.reviews_available_now = len((reviews[0].get("subject_ids") or [])) if reviews else 0

    assignments = [a.get("data") or {} for a in (raw.get("assignments") or {}).get("data") or []]
    counts: dict[str, int] = {b: 0 for b in ("apprentice", "guru", "master", "enlightened", "burned")}
    for a in assignments:
        bucket = STAGE_BUCKETS.get(int(a.get("srs_stage") or 0))
        if bucket:
            counts[bucket] += 1
    p.stage_counts = counts

    subjects = _subject_index(raw.get("subjects") or {})
    stats = {int(r["data"]["subject_id"]): r["data"] for r in (raw.get("review_statistics") or {}).get("data") or []
             if isinstance(r.get("data"), dict) and "subject_id" in r["data"]}

    recent = sorted((a for a in assignments if a.get("unlocked_at")), key=lambda a: a["unlocked_at"], reverse=True)
    for a in recent[:RECENT_LIMIT]:
        sid = int(a["subject_id"])
        sub = subjects.get(sid)
        if sub:
            st = stats.get(sid, {})
            p.recent_unlocks.append(Vocab(**sub, srs_stage=int(a.get("srs_stage") or 0),
                                          incorrect=int(st.get("meaning_incorrect") or 0) + int(st.get("reading_incorrect") or 0)))

    # Leeches: low stage + most incorrect answers (review_statistics filtered to <80% correct upstream).
    stage_by_sid = {int(a["subject_id"]): int(a.get("srs_stage") or 0) for a in assignments if "subject_id" in a}
    leech_rows = sorted(
        (st for sid, st in stats.items() if stage_by_sid.get(sid, 9) <= 6),
        key=lambda st: int(st.get("meaning_incorrect") or 0) + int(st.get("reading_incorrect") or 0),
        reverse=True,
    )
    for st in leech_rows[:LEECH_LIMIT]:
        sid = int(st["subject_id"])
        sub = subjects.get(sid)
        if sub:
            p.leeches.append(Vocab(**sub, srs_stage=stage_by_sid.get(sid, 0),
                                   incorrect=int(st.get("meaning_incorrect") or 0) + int(st.get("reading_incorrect") or 0)))

    kanji = {int(item["id"]): (item.get("data") or {}).get("characters")
             for item in (raw.get("kanji_subjects") or {}).get("data") or [] if item.get("id") is not None}
    passed = (x.get("data") or {} for x in (raw.get("kanji_assignments") or {}).get("data") or [])
    p.known_kanji = sorted({kanji[int(a["subject_id"])] for a in passed
                            if a.get("passed_at") and kanji.get(int(a.get("subject_id") or -1))})
    return p


# ------------------------------------------------------------------ fetch
def _page_all(client: SrsClient, path: str, params: dict | None, max_pages: int = 5) -> dict[str, Any]:
    """Follow `pages.next_url` (same origin) up to max_pages; returns a merged payload."""
    merged: dict[str, Any] = {"data": []}
    next_path: str | None = path
    next_params = params
    for _ in range(max_pages):
        if not next_path:
            break
        payload = client.get(next_path, next_params)
        merged["data"].extend(payload.get("data") or [])
        merged.setdefault("total_count", payload.get("total_count"))
        nxt = (payload.get("pages") or {}).get("next_url")
        if not nxt:
            break
        # next_url is absolute on the pinned origin; keep only path+query (client re-pins the origin).
        _, _, rest = nxt.partition("api.wanikani.com")
        next_path, _, query = rest.partition("?")
        next_params = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv) if query else None
    return merged


def fetch_raw(client: SrsClient) -> dict[str, Any]:
    raw: dict[str, Any] = {"_errors": {}}

    def step(name, fn):
        try:
            raw[name] = fn()
        except Exception as e:
            raw["_errors"][name] = f"{type(e).__name__}: {e}"

    step("user", lambda: client.get("/v2/user"))
    step("summary", lambda: client.get("/v2/summary"))
    step("assignments", lambda: _page_all(client, "/v2/assignments", {"subject_types": "vocabulary", "started": "true"}))
    step("review_statistics", lambda: _page_all(client, "/v2/review_statistics",
                                                {"subject_types": "vocabulary", "percentages_less_than": "80"}))
    assignments = [a.get("data") or {} for a in (raw.get("assignments") or {}).get("data") or []]
    recent = sorted((a for a in assignments if a.get("unlocked_at")), key=lambda a: a["unlocked_at"], reverse=True)
    ids = {int(a["subject_id"]) for a in recent[:RECENT_LIMIT]}
    stats = (raw.get("review_statistics") or {}).get("data") or []
    ids |= {int(r["data"]["subject_id"]) for r in stats[:LEECH_LIMIT * 2] if isinstance(r.get("data"), dict)}
    if ids:
        step("subjects", lambda: client.get("/v2/subjects", {"ids": ",".join(map(str, sorted(ids)))}))
    # The chat's furigana (spec §8b): which kanji the student has passed, and their characters.
    step("kanji_assignments", lambda: _page_all(client, "/v2/assignments", {"subject_types": "kanji", "started": "true"}))
    level = int(((raw.get("user") or {}).get("data") or {}).get("level") or 0)
    if level:
        step("kanji_subjects", lambda: _page_all(client, "/v2/subjects",
                                                 {"types": "kanji", "levels": ",".join(map(str, range(1, level + 1)))}))
    return raw


def fetch(client: SrsClient) -> tuple[WaniKaniProfile, dict[str, str]]:
    raw = fetch_raw(client)
    return parse(raw), dict(raw.get("_errors") or {})
