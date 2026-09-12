"""One-off capture of real Bunpro read-endpoint responses for fixture pinning (ROADMAP V0.2/M1).

Reads the token from settings.json (never printed). GETs each read endpoint through the
GET-only client and writes raw JSON to .cache/bunpro_capture/<name>.json (git-ignored).
Prints only: status, top-level keys, byte size. Never prints response bodies.
"""
from __future__ import annotations

import json
import sys

from backend import config, constants
from backend.srs import bunpro as bp
from backend.srs.http import SrsClient, SrsError

VARIANTS: list[tuple[str, str, dict | None]] = [
    ("user", constants.BUNPRO_READ_ENDPOINTS["user"], None),
    ("due", constants.BUNPRO_READ_ENDPOINTS["due"], None),
    ("queue", constants.BUNPRO_READ_ENDPOINTS["queue"], None),
    ("jlpt_progress", constants.BUNPRO_READ_ENDPOINTS["jlpt_progress"], None),
    ("srs_overview", constants.BUNPRO_READ_ENDPOINTS["srs_overview"], None),
    ("ghost_grammar", constants.BUNPRO_READ_ENDPOINTS["ghost_level_details"], {"reviewable_type": "Grammar"}),
    # `level` is NAMED (beginner|adept|seasoned|expert|master): a numeric level returns HTTP 500
    # (verified live 2026-09-09, see srs/bunpro.py). These are the levels the launch fetch reads.
    *[(f"srs_level_{level}_grammar", constants.BUNPRO_READ_ENDPOINTS["srs_level_details"],
       {"reviewable_type": "Grammar", "level": level}) for level in bp.IN_PLAY_LEVELS],
    ("forecast_daily", constants.BUNPRO_READ_ENDPOINTS["forecast_daily"], None),
]


def _shape(obj, depth=0) -> str:
    if isinstance(obj, dict):
        keys = list(obj.keys())
        return "{" + ", ".join(f"{k}: {_shape(obj[k], depth + 1)}" for k in keys[:12]) + (", …" if len(keys) > 12 else "") + "}"
    if isinstance(obj, list):
        return f"[{len(obj)} x {_shape(obj[0], depth + 1) if obj else '∅'}]"
    return type(obj).__name__


def main() -> int:
    cfg = config.load()
    token = cfg.BUNPRO_API_TOKEN
    if not token:
        print("capture-bunpro: BUNPRO_API_TOKEN is not set in settings.json (settings page / key bunpro_api_token)", file=sys.stderr)
        return 2
    out_dir = cfg.path("CACHE_DIR") / "bunpro_capture"
    out_dir.mkdir(parents=True, exist_ok=True)
    client = SrsClient("bunpro", token)
    failures = 0
    for name, path, params in VARIANTS:
        full = constants.BUNPRO_API_PREFIX + path
        try:
            data = client.get(full, params)
        except SrsError as e:
            failures += 1
            print(f"  {name:<22} FAIL  {e}")
            if e.status_code == 401:
                print("    -> token rejected. Bunpro -> Settings -> API -> Account API Token, then settings page.")
            continue
        raw = json.dumps(data, ensure_ascii=False)
        (out_dir / f"{name}.json").write_text(raw, encoding="utf-8")
        print(f"  {name:<22} OK    {len(raw):>7} B  {_shape(data)[:160]}")
    print(f"capture-bunpro: wrote to {out_dir.relative_to(config.REPO_ROOT).as_posix()}/ ; {failures} failure(s)")
    return 1 if failures == len(VARIANTS) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except config.ConfigError as exc:      # a malformed settings.json: one line, not a traceback
        sys.exit(str(exc))
