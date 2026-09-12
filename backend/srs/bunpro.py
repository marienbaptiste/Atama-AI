"""Bunpro fetcher — READ-ONLY (spec §0/§5, ADR-021/023). Shapes pinned live 2026-09-09.

Endpoints (all GET through SrsClient, all under /api/frontend):
  /user                                   -> user.data.attributes.{username, level (Bunpro XP level, not JLPT)}
  /user/due                               -> {total_due_grammar, total_due_vocab}
  /user_stats/jlpt_progress_mixed         -> {grammar: {"5".."1": {beginner, adept, seasoned, expert, master, total_count}}, vocab: {...}}
  /user_stats/srs_level_overview          -> {grammar: {beginner, adept, seasoned, expert, master, ghost, self_study}, vocab: {...}}
                                             (verified and captured for the fixtures; NOT fetched at launch -
                                             nothing in the profile reads it, and every call costs a second)
  /user_stats/srs_ghost_level_details?reviewable_type=Grammar
                                          -> {type: "ghost", reviews: {data: [ghost_review], included: [reviewable]}}
  /user_stats/srs_level_details?reviewable_type=Grammar&level=<beginner|adept|seasoned|expert|master>
                                          -> {type, reviews: {data: [review], included: [reviewable]}, pagy}
                                             (numeric `level` -> HTTP 500; omitted `level` -> HTTP 500)
  /user_stats/forecast_daily              -> {grammar: {later, tomorrow, "YYYY-MM-DD": n, ...}, vocab: {...}}
Reviewable (included) attributes: {id, slug, furigana, title, meaning, level: "JLPT4", type_pascal, type_snake}.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from backend import constants
from backend.srs.http import HostViolation, ReadOnlyViolation, SrsClient

P = constants.BUNPRO_API_PREFIX
E = constants.BUNPRO_READ_ENDPOINTS
SRS_LEVELS = ("beginner", "adept", "seasoned", "expert", "master")
#: What still counts as "being learned" rather than settled: everything the student has not taken
#: past Seasoned. She works through all of it, weakest first — ghosts, then beginner, then adept,
#: then seasoned (user, 2026-09-12: "all the forms I haven't Guru'd or mastered, with a preference
#: for the all new fresh stuff").
IN_PLAY_LEVELS = ("beginner", "adept", "seasoned")
#: Kept per level. The prompt shows the first handful of each; the chat matches every one.
LEVEL_LIMIT = 40
JLPT_KEYS = ("5", "4", "3", "2", "1")


@dataclass
class GrammarPoint:
    title: str
    meaning: str
    jlpt: str            # e.g. "N4"
    srs: str             # "ghost" | "beginner" | "adept" | ...
    streak: int = 0
    next_review: str = ""


@dataclass
class BunproProfile:
    username: str = ""
    due_grammar: int = 0
    due_vocab: int = 0
    jlpt_grammar: dict[str, dict[str, int]] = field(default_factory=dict)   # "N4" -> {learned, total}
    ghosts: list[GrammarPoint] = field(default_factory=list)
    weak_grammar: list[GrammarPoint] = field(default_factory=list)         # beginner-stage points
    #: Everything still in the SRS rather than settled, weakest first: ghosts, beginner, adept,
    #: seasoned. What she is asked to work through, and what the chat matches (user, 2026-09-12).
    in_play: list[GrammarPoint] = field(default_factory=list)
    forecast_tomorrow_grammar: int = 0
    forecast_later_grammar: int = 0

    def current_jlpt(self) -> str:
        """The JLPT level the student is actively working: the one with the most grammar in SRS
        (ties -> lower level). A student on the N4 deck with 2 stray N5 points is 'N4'.
        Pinned against the live profile 2026-09-09 (N5 2/132, N4 60/185 -> N4)."""
        best, best_n = "N5", -1
        for n in ("N5", "N4", "N3", "N2", "N1"):
            row = self.jlpt_grammar.get(n)
            if row and row["learned"] > best_n:
                best, best_n = n, row["learned"]
        return best

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ parsers
def _reviewables(payload: dict) -> dict[str, dict]:
    inc = _d(payload.get("reviews")).get("included")
    return {str(r.get("id")): _d(r.get("attributes")) for r in (inc if isinstance(inc, list) else []) if isinstance(r, dict)}


def _points(payload: dict, srs: str, limit: int) -> list[GrammarPoint]:
    by_id = _reviewables(payload)
    out: list[GrammarPoint] = []
    data = _d(payload.get("reviews")).get("data")
    for rev in (data if isinstance(data, list) else []):
        if not isinstance(rev, dict):
            continue
        a = _d(rev.get("attributes"))
        if a.get("reviewable_type") not in (None, "GrammarPoint"):
            continue
        r = by_id.get(str(a.get("reviewable_id")), {})
        if not r.get("title"):
            continue
        out.append(GrammarPoint(
            title=str(r.get("title", "")),
            meaning=str(r.get("meaning", "")),
            jlpt=str(r.get("level", "")).replace("JLPT", "N"),
            srs=srs,
            streak=_i(a.get("streak")),
            next_review=str(a.get("next_review") or ""),
        ))
        if len(out) >= limit:
            break
    return out


def _d(x: Any) -> dict:
    """Coerce to dict: upstream is unofficial and may return anything."""
    return x if isinstance(x, dict) else {}


def _i(x: Any) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def parse(raw: dict[str, Any]) -> BunproProfile:
    """Build a profile from the raw endpoint payloads {name: payload}. Missing/garbage keys degrade to defaults."""
    raw = _d(raw)
    p = BunproProfile()
    user = _d(_d(_d(raw.get("user")).get("user")).get("data"))
    p.username = str(_d(user.get("attributes")).get("username") or "")
    due = _d(raw.get("due"))
    p.due_grammar = _i(due.get("total_due_grammar"))
    p.due_vocab = _i(due.get("total_due_vocab"))
    jl = _d(_d(raw.get("jlpt_progress")).get("grammar"))
    for k in JLPT_KEYS:
        row = jl.get(k)
        if isinstance(row, dict):
            learned = sum(_i(row.get(lvl)) for lvl in SRS_LEVELS)
            p.jlpt_grammar[f"N{k}"] = {"learned": learned, "total": _i(row.get("total_count"))}
    p.ghosts = _points(_d(raw.get("ghost_grammar")), "ghost", 15)
    p.weak_grammar = _points(_d(raw.get("srs_level_beginner_grammar")), "beginner", 15)
    seen = set()
    for level in ("ghost", *IN_PLAY_LEVELS):
        key = "ghost_grammar" if level == "ghost" else f"srs_level_{level}_grammar"
        for point in _points(_d(raw.get(key)), level, LEVEL_LIMIT):
            if point.title and point.title not in seen:
                seen.add(point.title)
                p.in_play.append(point)
    fc = _d(_d(raw.get("forecast_daily")).get("grammar"))
    p.forecast_tomorrow_grammar = int(fc.get("tomorrow") or 0)
    p.forecast_later_grammar = int(fc.get("later") or 0)
    return p


# ------------------------------------------------------------------ fetch
def fetch_raw(client: SrsClient) -> dict[str, Any]:
    """GET every endpoint the profile needs. Individual failures are recorded, not raised -
    except a Golden Rule violation, which is never swallowed (spec §0)."""
    raw: dict[str, Any] = {"_errors": {}}
    # Eight calls, ONLY at app launch / manual refresh (spec §5 fetch policy). With
    # BUNPRO_MIN_INTERVAL_S spacing this fits the 10 s budget. The MCP tools never call Bunpro;
    # they read this snapshot. adept and seasoned joined beginner on 2026-09-12 (user: "she must
    # use all the forms I haven't Guru'd or mastered"): those three levels are what is still in
    # the student's SRS rather than settled, and the tutor is asked to work through all of them.
    calls = [
        ("user", E["user"], None),
        ("due", E["due"], None),
        ("jlpt_progress", E["jlpt_progress"], None),
        ("ghost_grammar", E["ghost_level_details"], {"reviewable_type": "Grammar"}),
        *[(f"srs_level_{level}_grammar", E["srs_level_details"],
           {"reviewable_type": "Grammar", "level": level}) for level in IN_PLAY_LEVELS],
        ("forecast_daily", E["forecast_daily"], None),
    ]
    for name, path, params in calls:
        try:
            raw[name] = client.get(P + path, params)
        except (ReadOnlyViolation, HostViolation):
            raise  # Golden Rule (spec §0): logged CRITICAL by the transport; the chip goes to `error`
        except Exception as e:  # SrsError or unexpected; never let one endpoint kill the profile
            raw["_errors"][name] = f"{type(e).__name__}: {e}"
    return raw


def fetch(client: SrsClient) -> tuple[BunproProfile, dict[str, str]]:
    raw = fetch_raw(client)
    return parse(raw), dict(raw.get("_errors") or {})
