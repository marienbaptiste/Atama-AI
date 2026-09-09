"""One-off capture of real WaniKani v2 responses for fixture pinning (ROADMAP V0.5/V0.9, M1).

Reads the token from settings.json (never printed). GETs the read endpoints the fetcher needs
through the GET-only client and writes raw JSON to .cache/wanikani_capture/<name>.json.
Prints only: status, shape, byte size, and the token's granted permissions (so the doctor's
read-only warning can be pinned against a real payload).
"""
from __future__ import annotations

import json
import sys

from backend import config
from backend.srs.http import SrsClient, SrsError

# WaniKani API v2, official docs. All GET. Assignments are paginated (pages_per_page=500).
VARIANTS: list[tuple[str, str, dict | None]] = [
    ("user", "/v2/user", None),
    ("summary", "/v2/summary", None),
    ("assignments_started_vocab", "/v2/assignments", {"subject_types": "vocabulary", "started": "true"}),
    ("assignments_recent_unlocks", "/v2/assignments", {"unlocked": "true", "subject_types": "vocabulary"}),
    ("review_statistics_vocab", "/v2/review_statistics", {"subject_types": "vocabulary", "percentages_less_than": "80"}),
    ("level_progressions", "/v2/level_progressions", None),
]


def _shape(obj) -> str:
    if isinstance(obj, dict):
        keys = list(obj.keys())
        return "{" + ", ".join(f"{k}: {_shape(obj[k])}" for k in keys[:10]) + (", …" if len(keys) > 10 else "") + "}"
    if isinstance(obj, list):
        return f"[{len(obj)} x {_shape(obj[0]) if obj else '∅'}]"
    return type(obj).__name__


def main() -> int:
    cfg = config.load()
    token = cfg.WANIKANI_TOKEN
    if not token:
        print("capture-wanikani: WANIKANI_TOKEN is not set in settings.json", file=sys.stderr)
        return 2
    out_dir = cfg.path("CACHE_DIR") / "wanikani_capture"
    out_dir.mkdir(parents=True, exist_ok=True)
    client = SrsClient("wanikani", token)
    failures = 0
    for name, path, params in VARIANTS:
        try:
            data = client.get(path, params)
        except SrsError as e:
            failures += 1
            print(f"  {name:<28} FAIL  {e}")
            if e.status_code == 401:
                print("    -> token rejected. wanikani.com -> Settings -> API Tokens (create with NO write permissions).")
            continue
        raw = json.dumps(data, ensure_ascii=False)
        (out_dir / f"{name}.json").write_text(raw, encoding="utf-8")
        extra = ""
        if name == "user":
            d = data.get("data", {}) if isinstance(data, dict) else {}
            extra = f"  level={d.get('level')} subscription={d.get('subscription', {}).get('active')}"
        print(f"  {name:<28} OK    {len(raw):>7} B  {_shape(data)[:140]}{extra}")
    print(f"capture-wanikani: wrote to {out_dir.relative_to(config.REPO_ROOT).as_posix()}/ ; {failures} failure(s)")
    return 1 if failures == len(VARIANTS) else 0


if __name__ == "__main__":
    sys.exit(main())
