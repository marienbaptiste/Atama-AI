"""Turn raw captures (.cache/*_capture/, git-ignored) into sanitised, committed golden fixtures.

Sanitisation: personal identifiers (usernames, user ids, profile/avatar URLs, account timestamps)
are replaced by value everywhere; heavy or copyrighted content fields (mnemonics, context
sentences, audio) are dropped; study items (grammar points, vocab) are public content and kept;
lists are truncated. Re-run after any capture; review the diff before commit.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from backend import config

FIXTURES = config.REPO_ROOT / "backend" / "tests" / "fixtures"
DROP_KEYS = {"meaning_mnemonic", "reading_mnemonic", "context_sentences", "pronunciation_audios",
             "document_url", "cover_image_url", "description", "avatar_url", "profile_url"}
NAME_KEYS = {"username", "name"}
#: Account state that is personal but not an identifier: neutralised by value so the committed
#: user fixtures carry the key structure and nothing about the student (spec §11 hygiene).
PLACEHOLDER_TS = "2020-01-01T00:00:00Z"
PLACEHOLDER_UUID = "00000000-0000-0000-0000-000000000000"   # the nil UUID: UUID-shaped, not v4, so check_secrets ignores it
BUNPRO_ZERO_KEYS = {"buncoin", "level", "next_level_xp", "prev_level_xp", "xp"}
BUNPRO_TS_KEYS = {"created_at", "updated_at"}


def _scrub(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in DROP_KEYS:
                continue
            if k in NAME_KEYS and isinstance(v, str):
                out[k] = "student"
            elif k == "user_id" and isinstance(v, int):
                out[k] = 0
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def _neutralise_user(service: str, data: dict) -> dict:
    """The `user` payloads: keep the shape, drop the account state (XP, cosmetics, dates, ids)."""
    if service == "wanikani":
        d = data.get("data") or {}
        d["id"], d["started_at"] = PLACEHOLDER_UUID, PLACEHOLDER_TS
        data["data_updated_at"] = PLACEHOLDER_TS
        return data
    u = (data.get("user") or {}).get("data") or {}
    u["id"] = "0"
    a = u.get("attributes") or {}
    for k in BUNPRO_ZERO_KEYS | {"id"}:
        if k in a:
            a[k] = 0
    for k in BUNPRO_TS_KEYS:
        if k in a:
            a[k] = PLACEHOLDER_TS
    for k, v in (("show_nsfw_content", "No"), ("deck_queue_ordering", []), ("inactive_warnings", []),
                 ("has_active_subscription", False), ("is_lifetime", False)):
        if k in a:
            a[k] = v
    if "active_cosmetics" in data:
        data["active_cosmetics"] = {"data": []}
    if "active_title" in data:
        data["active_title"] = ""
    return data


def _truncate_lists(obj, limit: int):
    if isinstance(obj, dict):
        return {k: _truncate_lists(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate_lists(x, limit) for x in obj[:limit]]
    return obj


def _identifiers(cache: Path) -> list[str]:
    """Personal identifier values to replace textually in every fixture."""
    ids: list[str] = []
    bp = cache / "bunpro_capture" / "user.json"
    if bp.exists():
        attrs = json.loads(bp.read_text(encoding="utf-8")).get("user", {}).get("data", {})
        for v in (attrs.get("id"), (attrs.get("attributes") or {}).get("id"), (attrs.get("attributes") or {}).get("username"),
                  (attrs.get("attributes") or {}).get("name")):
            if v not in (None, ""):
                ids.append(str(v))
    wk = cache / "wanikani_capture" / "user.json"
    if wk.exists():
        d = json.loads(wk.read_text(encoding="utf-8")).get("data", {})
        for v in (d.get("id"), d.get("username"), d.get("profile_url"), d.get("started_at")):
            if v not in (None, ""):
                ids.append(str(v))
    # longest first so substrings don't shadow
    return sorted({i for i in ids if len(i) >= 3}, key=len, reverse=True)


RECENT_SUBJECT_IDS: list[int] = []  # filled while processing assignments, consumed by subjects

PLAN = {
    "bunpro": {  # capture name -> (fixture name, list limit)
        "user": ("user", 5), "due": ("due", 5), "queue": ("queue", 5),
        "jlpt_progress": ("jlpt_progress", 5), "srs_overview": ("srs_overview", 5),
        "ghost_grammar": ("ghost_grammar", 5), "forecast_daily": ("forecast_daily", 5),
        "srs_level_beginner_grammar": ("srs_level_beginner_grammar", 6),
        "srs_level_adept_grammar": ("srs_level_adept_grammar", 6),
        "srs_level_seasoned_grammar": ("srs_level_seasoned_grammar", 6),
    },
    "wanikani": {
        "user": ("user", 5), "summary": ("summary", 5),
        "assignments_started_vocab": ("assignments_vocab", 40),
        "review_statistics_vocab": ("review_statistics_vocab", 20),
        "level_progressions": ("level_progressions", 10),
        "subjects_recent30": ("subjects_recent", 15),
    },
}


def main() -> int:
    cache = config.load().path("CACHE_DIR")
    idents = _identifiers(cache)
    written = 0
    for service, plan in PLAN.items():
        src_dir = cache / f"{service}_capture"
        dst_dir = FIXTURES / service
        dst_dir.mkdir(parents=True, exist_ok=True)
        for cap_name, (fx_name, limit) in plan.items():
            src = src_dir / f"{cap_name}.json"
            if not src.exists():
                print(f"  skip {service}/{cap_name} (not captured)")
                continue
            data = _scrub(json.loads(src.read_text(encoding="utf-8")))
            if cap_name == "user":
                data = _neutralise_user(service, data)
            if service == "wanikani" and cap_name == "assignments_started_vocab":
                # keep the MOST RECENT unlocks so they line up with the subjects fixture
                data["data"] = sorted(data["data"], key=lambda d: (d.get("data") or {}).get("unlocked_at") or "", reverse=True)
                recent_ids = [int(d["data"]["subject_id"]) for d in data["data"][:PLAN["wanikani"]["subjects_recent30"][1]]]
                RECENT_SUBJECT_IDS.extend(recent_ids)
            if service == "wanikani" and cap_name == "subjects_recent30":
                data["data"] = [s for s in data["data"] if int(s.get("id", -1)) in set(RECENT_SUBJECT_IDS)]
            data = _truncate_lists(data, limit)
            text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
            for ident in idents:
                text = text.replace(ident, "0" if ident.isdigit() else "student")
            (dst_dir / f"{fx_name}.json").write_text(text, encoding="utf-8")
            written += 1
            print(f"  {service}/{fx_name}.json ({len(text)} B)")
    leftovers = [i for i in idents if any((FIXTURES / s / f).read_text(encoding="utf-8").find(i) != -1
                                          for s in PLAN for f in [p.name for p in (FIXTURES / s).glob("*.json")])]
    if leftovers:
        print("make-fixtures: identifier(s) still present — inspect manually", file=sys.stderr)
        return 1
    print(f"make-fixtures: {written} fixture(s) written, {len(idents)} identifier(s) scrubbed — review the diff before committing")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except config.ConfigError as exc:      # a malformed settings.json: one line, not a traceback
        sys.exit(str(exc))
