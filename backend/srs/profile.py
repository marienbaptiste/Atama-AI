"""Student Profile renderer (spec §5, ADR-011): ≤ 600 tokens, zero/one/both sources.

Also the session-start orchestration: parallel fetch inside a budget, disk cache with TTL,
status reporting (`disabled` / `syncing` / `ok` / `stale` / `error`).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.srs import bunpro as bp
from backend.srs import wanikani as wk
from backend.srs.cache import cache_load, cache_store
from backend.srs.http import HostViolation, ReadOnlyViolation, SrsClient
from backend.status import StatusRegistry

_log = logging.getLogger(__name__)

MAX_TOKENS = 600


def estimate_tokens(text: str) -> int:
    """Conservative estimate: Japanese ~1 token/char, ASCII ~1 token/4 chars. Over-counts on purpose."""
    jp = sum(1 for ch in text if ord(ch) > 0x2E7F)
    ascii_chars = len(text) - jp
    return jp + (ascii_chars + 3) // 4


@dataclass
class StudentProfile:
    wanikani: wk.WaniKaniProfile | None = None
    bunpro: bp.BunproProfile | None = None
    sources: dict[str, str] = field(default_factory=dict)  # service -> state
    errors: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wanikani": self.wanikani.to_dict() if self.wanikani else None,
            "bunpro": self.bunpro.to_dict() if self.bunpro else None,
            "sources": self.sources,
            "errors": self.errors,
        }


# ------------------------------------------------------------------ render
def _vocab_line(v: wk.Vocab) -> str:
    return f"{v.characters}（{v.reading}）{v.meaning}"


#: How many of the still-being-learned grammar points reach the prompt. Past the first ten only
#: their titles go in, which is what she needs to use the form (spec §5, ADR-011's budget).
GRAMMAR_SHOWN = 34


def _gp_line(g: bp.GrammarPoint) -> str:
    return f"{g.title}（{g.meaning}）"


def render(profile: StudentProfile) -> str:
    lines: list[str] = []
    w, b = profile.wanikani, profile.bunpro
    if not w and not b:
        lines.append("No SRS data available (WaniKani and Bunpro not configured). Assume an early-beginner student; ask what they have studied.")
        return "\n".join(lines)

    if w:
        sc = w.stage_counts
        lines.append(f"WaniKani level {w.level}. Vocab in SRS: apprentice {sc.get('apprentice', 0)}, guru {sc.get('guru', 0)}, master {sc.get('master', 0)}+ ."
                     f" Reviews waiting now: {w.reviews_available_now}.")
        if w.recent_unlocks:
            lines.append("Recent WaniKani vocab (prefer these): " + "、".join(_vocab_line(v) for v in w.recent_unlocks[:30]))
        if w.leeches:
            lines.append("WaniKani leeches (reuse naturally): " + "、".join(_vocab_line(v) for v in w.leeches[:15]))
    else:
        lines.append("WaniKani: not connected.")

    if b:
        jl = b.jlpt_grammar
        prog = ", ".join(f"{n} {jl[n]['learned']}/{jl[n]['total']}" for n in ("N5", "N4", "N3", "N2", "N1") if n in jl and (jl[n]["learned"] or n in ("N5", "N4")))
        lines.append(f"Bunpro grammar: studying {b.current_jlpt()}. Progress: {prog}. Due now: {b.due_grammar} grammar, {b.due_vocab} vocab.")
        if b.ghosts:
            lines.append("Bunpro ghost reviews (their weakest grammar — work these in): " + "、".join(_gp_line(g) for g in b.ghosts[:15]))
        # Everything still in their SRS, weakest first, so she can work through all of it rather
        # than the same two ghosts (user, 2026-09-12). Titles only past the first few: the point
        # is coverage, and a title is enough for her to use the form.
        rest = [g for g in b.in_play if g.srs != "ghost"] or b.weak_grammar
        if rest:
            lines.append("Grammar they are still learning, weakest first — USE THESE, and prefer "
                         "the newest: " + "、".join(_gp_line(g) for g in rest[:10])
                         + ("、" + "、".join(g.title for g in rest[10:GRAMMAR_SHOWN]) if len(rest) > 10 else ""))
    else:
        lines.append("Bunpro: not connected.")

    text = "\n".join(lines)
    # Trim the long lists first if we are over budget.
    while estimate_tokens(text) > MAX_TOKENS:
        trimmed = False
        for i, line in enumerate(lines):
            if "、" in line:
                head, _, tail = line.rpartition("、")
                if tail:
                    lines[i] = head
                    trimmed = True
                    break
        if not trimmed:
            break
        text = "\n".join(lines)
    return text


# ------------------------------------------------------------------ session-start orchestration
def snapshot_load(path: Path) -> tuple[dict | None, str]:
    """Read a stored snapshot -> (raw, fetched_at ISO). Accepts the legacy bare-raw layout."""
    data, _ = cache_load(path, ttl_s=float("inf"))
    if not isinstance(data, dict):
        return None, ""
    if "raw" in data and "fetched_at" in data:
        return data["raw"], str(data["fetched_at"])
    return data, ""


def _one(service: str, token: str, cache_path: Path, min_age_s: float, registry: StatusRegistry,
         deadline: float, client_kwargs: dict | None = None, force: bool = False,
         cancel: threading.Event | None = None):
    """Fetch one service — ONLY at app launch or manual refresh (spec §5 fetch policy).

    - force=False (launch): fetch once and store the snapshot. `min_age_s` (config
      `SRS_CACHE_TTL_S`) re-uses a snapshot younger than that instead; its default is 0 — every
      launch fetches the latest data (user directive 2026-09-12), the guard is opt-in. A
      snapshot flagged `partial` (some endpoints failed last time) is never re-used: the next
      launch fetches again, however young it is — that is the only "retry" there is (ADR-024).
    - force=True (manual refresh): always fetch once.
    - A fetch that partly failed is served as `ok` with the failures in `last_error`; one the
      service rate-limited (429) is served as `stale` — incomplete because the service said stop.
    - A Golden Rule violation (spec §0) is never swallowed: CRITICAL log, chip `error`.
    - `cancel` is set by build() when the budget ran out: the client then refuses further
      requests and this worker stores and reports nothing (build() already reported).
    Reports status only if still before `deadline` (a late thread must not flip a chip that
    build() already marked as timed out)."""
    import datetime as _dt
    import time as _t

    def report(*a, **k):
        if _t.monotonic() <= deadline:
            registry.report(service, *a, **k)

    if not token:
        report("disabled", "no token")
        return None, "disabled", {}
    report("syncing")
    cached, fresh = cache_load(cache_path, min_age_s)
    partial_snapshot = isinstance(cached, dict) and bool(cached.get("partial"))
    cached_raw, fetched_at = snapshot_load(cache_path)
    parse = wk.parse if service == "wanikani" else bp.parse
    if cached_raw is not None and fresh and not force and not partial_snapshot:
        prof = parse(cached_raw)
        report("ok", _detail(service, prof) + f" (synced {_hhmm(fetched_at)})")
        return prof, "ok", {}
    client = SrsClient(service, token, cancel=cancel, **(client_kwargs or {}))  # type: ignore[arg-type]
    try:
        raw = (wk.fetch_raw if service == "wanikani" else bp.fetch_raw)(client)
    except (ReadOnlyViolation, HostViolation) as e:
        msg = f"{type(e).__name__}: {e}"
        _log.critical("GOLDEN RULE (spec 0) violated during the %s fetch - aborted: %s", service, msg)
        # Not guarded by the deadline: a violation must always reach the chip.
        registry.report(service, "error", "read-only violation - fetch aborted", last_error=msg)
        return None, "error", {"violation": msg}
    if cancel is not None and cancel.is_set():
        return None, "error", {"timeout": "cancelled by build()"}  # build() reported; store nothing
    errors = dict(raw.pop("_errors", {}) or {})
    warnings = dict(raw.pop("_warnings", {}) or {})
    rate_limited = client.rate_limited
    problems = "; ".join(dict.fromkeys([*errors.values(), *warnings.values()]))
    got_core = ("user" in raw) and (("assignments" in raw) if service == "wanikani" else ("jlpt_progress" in raw))
    if got_core:
        now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        partial = bool(errors)
        cache_store(cache_path, {"fetched_at": now, "service": service, "raw": raw, "partial": partial})
        prof = parse(raw)
        if rate_limited:
            report("stale", _detail(service, prof) + f" (synced {_hhmm(now)}, incomplete: rate limited)", last_error=problems)
            return prof, "stale", errors
        if partial:
            report("ok", _detail(service, prof) + f" (synced {_hhmm(now)}, partial: {len(errors)} endpoint(s) failed,"
                   " fetched again at next launch)", last_error=problems)
            return prof, "ok", errors
        report("ok", _detail(service, prof) + f" (synced {_hhmm(now)})", last_error=problems)
        return prof, "ok", errors
    why = "rate limited" if rate_limited else "fetch failed"
    if cached_raw is not None:
        prof = parse(cached_raw)
        report("stale", _detail(service, prof) + f" (snapshot {_hhmm(fetched_at)}, {why})", last_error=problems)
        return prof, "stale", errors
    report("error", f"{why}, no snapshot", last_error=problems)
    return None, "error", errors


def _hhmm(iso: str) -> str:
    """'2026-09-09T10:15:00+00:00' -> local 'HH:MM'; '' -> 'unknown time'."""
    import datetime as _dt
    if not iso:
        return "unknown time"
    try:
        return _dt.datetime.fromisoformat(iso).astimezone().strftime("%H:%M")
    except ValueError:
        return "unknown time"


def _detail(service: str, prof) -> str:
    if service == "wanikani":
        return f"level {prof.level}, {len(prof.recent_unlocks)} recent, {len(prof.leeches)} leeches"
    return f"{prof.current_jlpt()}, {len(prof.ghosts)} ghosts, due {prof.due_grammar}"


def _stale_or_error(service: str, cache_path: Path, registry: StatusRegistry, budget_s: float):
    """A fetch that ran out of budget still has a snapshot to fall back on.

    Spec §5b defines `stale` as "serving cache; last fetch failed" and reserves `error` for
    "no cache, fetch failed" — so a timeout with a snapshot on disk is `stale`, not `error`.
    Dropping the source instead would silently strip Sensei's profile for the whole session.
    """
    detail = f"timed out after {budget_s:.0f}s"
    errors = {"timeout": f"> {budget_s}s"}
    cached_raw, fetched_at = snapshot_load(cache_path)
    if cached_raw is None:
        registry.report(service, "error", f"{detail}, no snapshot")
        return None, "error", errors
    try:
        prof = (wk.parse if service == "wanikani" else bp.parse)(cached_raw)
    except Exception as exc:  # a corrupt snapshot is no better than none
        registry.report(service, "error", f"{detail}, snapshot unreadable",
                        last_error=f"{type(exc).__name__}: {exc}")
        return None, "error", errors
    registry.report(service, "stale",
                    f"{_detail(service, prof)} (snapshot {_hhmm(fetched_at)}, {detail})")
    return prof, "stale", errors


def build(wanikani_token: str, bunpro_token: str, cache_dir: Path, min_age_s: float, budget_s: float,
          registry: StatusRegistry, client_overrides: dict[str, dict] | None = None,
          force: bool = False) -> StudentProfile:
    """Fetch both sources in parallel, each within its own `budget_s`. Never raises. A source
    that runs out of budget falls back to its last snapshot as `stale` (spec §5b) rather than
    being dropped. Called at app launch
    (force=False) and on manual refresh (force=True) — nowhere else (spec §5 fetch policy).
    `client_overrides` maps service -> extra SrsClient kwargs (tests: transport=, sleep=)."""
    import time as _t

    client_overrides = client_overrides or {}
    profile = StudentProfile()
    jobs = {
        "wanikani": (wanikani_token, cache_dir / "wanikani.json"),
        "bunpro": (bunpro_token, cache_dir / "bunpro.json"),
    }
    # `budget_s` is PER SERVICE, not a pot the services share. Futures are collected in order,
    # so one absolute deadline let a slow WaniKani starve Bunpro of the time it needed
    # (observed 2026-09-09: wanikani ok, bunpro "timed out after 10s" — while that same run
    # went on to write bunpro.json). The late-report guard has to cover the worst case, which
    # is every service spending its full budget.
    deadline = _t.monotonic() + budget_s * len(jobs)
    # Cooperative cancellation: a worker that outlives its budget is told to stop, and the
    # client refuses its next request. `shutdown(wait=False)` alone left it fetching in the
    # background while a manual Refresh started a second client against the same service.
    cancels = {svc: threading.Event() for svc in jobs}
    ex = cf.ThreadPoolExecutor(max_workers=2)
    try:
        futs = {svc: ex.submit(_one, svc, tok, path, min_age_s, registry, deadline, client_overrides.get(svc), force,
                               cancels[svc])
                for svc, (tok, path) in jobs.items()}
        for svc, fut in futs.items():
            try:
                prof, state, errors = fut.result(timeout=budget_s)   # per service (see above)
            except cf.TimeoutError:
                # The worker stops at its next request boundary and stores nothing; whatever it
                # already has is not worth a snapshot that races the next fetch.
                cancels[svc].set()
                prof, state, errors = _stale_or_error(svc, jobs[svc][1], registry, budget_s)
            except Exception as e:  # pragma: no cover - defensive
                registry.report(svc, "error", "unexpected", last_error=f"{type(e).__name__}: {e}")
                prof, state, errors = None, "error", {"unexpected": f"{type(e).__name__}: {e}"}
            setattr(profile, svc, prof)
            profile.sources[svc] = state
            if errors:
                profile.errors[svc] = errors
    finally:
        ex.shutdown(wait=False, cancel_futures=True)  # do not block startup on a hung fetch
    try:
        registry.dump(cache_dir / "status.json")  # persisted so the last known state survives a restart
    except OSError:
        pass
    return profile


def debug_snapshot(profile: StudentProfile, rendered: str, log_dir: Path, date: str) -> Path:
    """Dump the profile to logs/profile-<date>.json (spec §5). Local file only; no SRS write."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"profile-{date}.json"
    path.write_text(json.dumps({"rendered": rendered, "tokens_estimate": estimate_tokens(rendered), **profile.to_dict()},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return path
