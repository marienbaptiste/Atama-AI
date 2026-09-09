"""Service status registry (spec §5b, ADR-019).

Every subsystem reports (service, state, detail, last_error) here. The registry sanitises
error text once — secrets registered via `register_secret` are masked before anything leaves.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

SERVICES = ("wanikani", "bunpro", "bunpro_mcp", "brain", "search", "voicevox", "stt")

STATES: dict[str, frozenset[str]] = {
    "wanikani": frozenset({"disabled", "syncing", "ok", "stale", "error"}),
    "bunpro": frozenset({"disabled", "syncing", "ok", "stale", "error"}),
    "bunpro_mcp": frozenset({"disabled", "starting", "connected", "failed", "used"}),
    # `brain`, not `claude`: the provider is named in the detail (ADR-027).
    "brain": frozenset({"starting", "ready", "thinking", "rate_limited", "fallback", "restarting", "error"}),
    "search": frozenset({"disabled", "ok", "down", "used"}),
    "voicevox": frozenset({"ok", "down"}),
    "stt": frozenset({"loading", "warm", "error"}),
}


@dataclass
class Status:
    service: str
    state: str
    detail: str = ""
    last_error: str = ""
    updated_at: float = field(default_factory=time.time)

    def as_message(self) -> dict:
        return {
            "type": "service_status",
            "service": self.service,
            "state": self.state,
            "detail": self.detail,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }


class StatusRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: dict[str, Status] = {}
        self._secrets: list[str] = []
        self._listeners: list[Callable[[Status], None]] = []

    # --- secrets -----------------------------------------------------------
    def register_secret(self, value: str) -> None:
        if value and len(value) >= 6 and value not in self._secrets:
            self._secrets.append(value)

    def sanitize(self, text: str) -> str:
        if not text:
            return text
        for s in self._secrets:
            text = text.replace(s, "***")
        # Belt and braces for header echoes in exception text.
        for marker in ("Bearer ", "Token token="):
            idx = text.find(marker)
            while idx != -1:
                end = idx + len(marker)
                while end < len(text) and not text[end].isspace() and text[end] not in "'\",;)":
                    end += 1
                text = text[: idx + len(marker)] + "***" + text[end:]
                idx = text.find(marker, idx + len(marker) + 3)
        return text

    # --- reporting ---------------------------------------------------------
    def report(self, service: str, state: str, detail: str = "", last_error: str = "") -> Status:
        if service not in STATES:
            raise ValueError(f"unknown service {service}")
        if state not in STATES[service]:
            raise ValueError(f"invalid state {state!r} for {service}")
        st = Status(service, state, self.sanitize(detail), self.sanitize(last_error))
        with self._lock:
            prev = self._status.get(service)
            changed = prev is None or (prev.state, prev.detail, prev.last_error) != (st.state, st.detail, st.last_error)
            self._status[service] = st
            listeners = list(self._listeners)
        if changed:
            for fn in listeners:
                fn(st)
        return st

    def get(self, service: str) -> Status | None:
        with self._lock:
            return self._status.get(service)

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {k: v.as_message() for k, v in self._status.items()}

    def subscribe(self, fn: Callable[[Status], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def dump(self, path) -> None:
        """Persist the snapshot (sanitised already) so the UI can show last-known state on restart."""
        import json
        from pathlib import Path
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=1), encoding="utf-8")

    def table(self) -> str:
        rows = ["service      state          detail"]
        for svc in SERVICES:
            st = self._status.get(svc)
            if st is None:
                rows.append(f"{svc:<12} (no report)")
                continue
            extra = f"  [{st.last_error}]" if st.last_error else ""
            rows.append(f"{svc:<12} {st.state:<14} {st.detail}{extra}")
        return "\n".join(rows)


registry = StatusRegistry()
