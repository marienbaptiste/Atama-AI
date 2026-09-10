"""What the settings panel may see and change (spec §11, ADR-022).

The page is generated from `config.SCHEMA`, so this module only shapes that schema for the
browser and validates what comes back. Two rules it exists to enforce:

* **A stored secret never returns to the browser.** Values go out through `Config.public_view`,
  where every secret is `{set, hint}`. An empty secret coming back means "leave it as it is" —
  the page never has the value, so it cannot send it back, and a blank box must not erase a token.
* **The panel says when it cannot win.** `.env` and `ATAMA_*` variables override `settings.json`
  (config.py resolution order). A value saved here for a key pinned there would be silently
  ignored at the next launch, so those keys are reported as `pinned` and the page locks them.

Keys in `LIVE` (the audio devices) apply to the running session the moment they are saved; the
rest apply at the next launch (live application of the others is M3's runtime subset).
"""
from __future__ import annotations

from typing import Any

from backend import config

#: Keys shown but never editable from the page, with the reason the page displays.
LOCKED: dict[str, str] = {
    "HOST": "loopback only (ADR-017)",
    "SETTINGS_FILE": "this is where the panel saves",
}

#: String keys with a closed set of values: the page shows a picker, the server rejects the rest.
CHOICES: dict[str, tuple[str, ...]] = {
    "TURN_MODE": ("ptt", "vad"),
    "SUBTITLES": ("jp", "off"),
    "CLAUDE_EFFORT": ("low", "medium", "high", "xhigh", "max"),
    "BRAIN_PROVIDER": ("claude-cli",),
}

#: Keys the running session applies the moment they are saved (the rest: next launch).
LIVE = frozenset({"AUDIO_INPUT_DEVICE", "AUDIO_OUTPUT_DEVICE", "TUTOR_PERSONA"})


def pinned(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Keys whose value comes from `.env` or the process environment, and which one."""
    out = {k: ".env" for k, v in config.read_dotenv(config.REPO_ROOT / ".env").items()
           if k in config.KEYS and v != ""}
    out.update({k: "environment" for k, v in config.env_overrides(environ).items() if v != ""})
    return out


def schema() -> list[dict[str, Any]]:
    out = []
    for s in config.SCHEMA:
        default = s.default
        if s.secret:
            default = ""                       # the default of a secret is empty; say nothing more
        out.append({
            "key": s.key, "group": s.group, "type": s.type.__name__, "default": default,
            "description": s.description, "secret": s.secret,
            "choices": list(CHOICES.get(s.key, ())), "locked": LOCKED.get(s.key, ""),
            "options": None, "live": s.key in LIVE,
        })
    return out


#: Keys whose picker lists the audio devices connected right now.
DEVICE_KEYS = {"AUDIO_INPUT_DEVICE": "input", "AUDIO_OUTPUT_DEVICE": "output"}


#: The device watcher's latest list (backend/device_watch.py). This process cannot re-scan while
#: its microphone is open, so its own view goes stale; the watcher's does not.
latest: dict[str, list[str]] | None = None


def device_options(fresh: bool = False) -> dict[str, list[str]]:
    """Names of the devices that can actually be opened, one entry per physical device.

    Windows lists each device once per host API, and MME truncates names to 31 characters, so the
    same microphone arrives as "Microphone (Chat-Audeze Maxwel" and "Microphone (Chat-Audeze
    Maxwell)". The longer spelling wins. The Sound Mapper and DirectSound's "Primary" alias are
    left out: they ARE the system default, which the picker offers as its own first entry.
    """
    if not fresh and latest is not None:
        return {kind: list(names) for kind, names in latest.items()}
    out: dict[str, list[str]] = {"input": [], "output": []}
    try:
        from backend import audio
        devices = audio.list_devices()
    except Exception:  # noqa: BLE001 - no audio backend: the picker simply offers the default
        return out
    for d in devices:
        if not d.usable or d.name.startswith(audio.MAPPER_PREFIX) or d.name in audio.DEFAULT_ALIASES:
            continue
        names = out[d.kind]
        if any(n.startswith(d.name) for n in names):
            continue
        names[:] = [n for n in names if not d.name.startswith(n)] + [d.name]
    return out


def snapshot(cfg: config.Config | None = None, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """The `settings` echo: schema, masked values, pinned keys, connected devices."""
    cfg = cfg or config.load()
    fields = schema()
    devices = device_options()
    for f in fields:
        if f["key"] in DEVICE_KEYS:
            f["options"] = devices[DEVICE_KEYS[f["key"]]]
    return {"values": cfg.public_view(), "fields": fields, "pinned": pinned(environ)}


def validate(updates: dict[str, Any], environ: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """(accepted, errors). Never raises on user input: every problem is a per-key message."""
    accepted: dict[str, Any] = {}
    errors: dict[str, str] = {}
    locked_by_env = pinned(environ)
    by_key = {s.key: s for s in config.SCHEMA}
    for key, value in updates.items():
        s = by_key.get(key)
        if s is None:
            errors[key] = "unknown setting"
            continue
        if key in LOCKED:
            errors[key] = "not editable here: " + LOCKED[key]
            continue
        if key in locked_by_env:
            errors[key] = f"set in {locked_by_env[key]}, which wins over this panel"
            continue
        if s.secret and (value is None or str(value).strip() == ""):
            continue                           # blank = unchanged; the page never had the value
        if isinstance(value, dict):            # a {set, hint} echo sent straight back
            errors[key] = "expected a value"
            continue
        try:
            coerced = config._coerce(s, value.strip() if isinstance(value, str) else value)
        except ValueError as exc:
            errors[key] = str(exc).split(": ", 1)[-1]
            continue
        if key in CHOICES and coerced not in CHOICES[key]:
            errors[key] = "one of: " + ", ".join(CHOICES[key])
            continue
        accepted[key] = coerced
    return accepted, errors


def apply(updates: dict[str, Any], settings_file=None, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Validate, persist what is valid, and return the echo with `saved` and `errors`."""
    accepted, errors = validate(updates, environ)
    if accepted:
        config.save(accepted, settings_file)
    echo = snapshot(config.load(settings_file), environ)
    echo.update({"saved": sorted(accepted), "errors": errors})
    return echo
