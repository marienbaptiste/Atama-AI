"""Student Profile renderer (spec §5, ADR-011): ≤ 600 tokens, zero/one/both sources.

Also the session-start orchestration: parallel fetch inside a budget, disk cache with TTL,
status reporting (`disabled` / `syncing` / `ok` / `stale` / `error`).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.srs import bunpro as bp
from backend.srs import wanikani as wk
from backend.srs.cache import cache_load, cache_store
from backend.srs.http import SrsClient
from backend.status import StatusRegistry

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
        if b.weak_grammar:
            lines.append("Grammar still at beginner stage: " + "、".join(_gp_line(g) for g in b.weak_grammar[:15]))
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
         deadline: float, client_kwargs: dict | None = None, force: bool = False):
    """Fetch one service — ONLY at app launch or manual refresh (spec §5 fetch policy).

    - force=False (launch): re-use the snapshot if it is younger than `min_age_s` (guards against
      hammering the APIs on rapid restarts); otherwise fetch once and store the snapshot.
    - force=True (manual refresh): always fetch once.
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
    cached_raw, fetched_at = snapshot_load(cache_path)
    parse = wk.parse if service == "wanikani" else bp.parse
    if cached_raw is not None and fresh and not force:
        prof = parse(cached_raw)
        report("ok", _detail(service, prof) + f" (synced {_hhmm(fetched_at)})")
        return prof, "ok", {}
    client = SrsClient(service, token, **(client_kwargs or {}))  # type: ignore[arg-type]
    raw = (wk.fetch_raw if service == "wanikani" else bp.fetch_raw)(client)
    errors = dict(raw.pop("_errors", {}) or {})
    got_core = ("user" in raw) and (("assignments" in raw) if service == "wanikani" else ("jlpt_progress" in raw))
    if got_core:
        now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        cache_store(cache_path, {"fetched_at": now, "service": service, "raw": raw})
        prof = parse(raw)
        report("ok", _detail(service, prof) + f" (synced {_hhmm(now)})", last_error="; ".join(errors.values()))
        return prof, "ok", errors
    if cached_raw is not None:
        prof = parse(cached_raw)
        report("stale", _detail(service, prof) + f" (snapshot {_hhmm(fetched_at)})", last_error="; ".join(errors.values()))
        return prof, "stale", errors
    report("error", "fetch failed, no snapshot", last_error="; ".join(errors.values()))
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


def build(wanikani_token: str, bunpro_token: str, cache_dir: Path, min_age_s: float, budget_s: float,
          registry: StatusRegistry, client_overrides: dict[str, dict] | None = None,
          force: bool = False) -> StudentProfile:
    """Fetch both sources in parallel within `budget_s`. Never raises. Called at app launch
    (force=False) and on manual refresh (force=True) — nowhere else (spec §5 fetch policy).
    `client_overrides` maps service -> extra SrsClient kwargs (tests: transport=, sleep=)."""
    import time as _t

    client_overrides = client_overrides or {}
    profile = StudentProfile()
    deadline = _t.monotonic() + budget_s
    jobs = {
        "wanikani": (wanikani_token, cache_dir / "wanikani.json"),
        "bunpro": (bunpro_token, cache_dir / "bunpro.json"),
    }
    ex = cf.ThreadPoolExecutor(max_workers=2)
    try:
        futs = {svc: ex.submit(_one, svc, tok, path, min_age_s, registry, deadline, client_overrides.get(svc), force)
                for svc, (tok, path) in jobs.items()}
        for svc, fut in futs.items():
            try:
                prof, state, errors = fut.result(timeout=max(0.0, deadline - _t.monotonic()))
            except cf.TimeoutError:
                registry.report(svc, "error", f"timed out after {budget_s:.0f}s")
                prof, state, errors = None, "error", {"timeout": f"> {budget_s}s"}
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
