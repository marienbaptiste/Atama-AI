"""Keep the computer from sleeping while the tutor runs (ADR-041, user request 2026-09-16).

System sleep only: a lesson mid-sentence must not suspend the machine, but the display is the
page's business — it holds a screen wake lock of its own (frontend/src/main.ts) — so a phone
lesson can run with the laptop lid dimmed. Never raises: an OS without the tool says so in one
line and the lesson goes on.

Windows: `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` on the calling thread, and
`ES_CONTINUOUS` alone to release it — so `set()` must be called from the same thread both times,
which the event loop's thread is. Linux: `systemd-inhibit` holding a block inhibitor around a
sleeping child. macOS: `caffeinate -i`.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Callable

#: kernel32 flags (Win32 `SetThreadExecutionState`), verified against the API reference 2026-09-16.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """`set(True)` blocks system sleep until `set(False)`; both return a one-line detail."""

    def __init__(self, platform: str = sys.platform,
                 execution_state: Callable[[int], int] | None = None,
                 popen: Callable[..., subprocess.Popen] | None = None,
                 which: Callable[[str], str | None] = shutil.which) -> None:
        self.platform = platform
        self._execution_state = execution_state
        self._popen = popen or subprocess.Popen
        self._which = which
        self.on = False
        self.detail = "sleep allowed"
        self._proc: subprocess.Popen | None = None

    def set(self, on: bool) -> str:
        if on == self.on:
            return self.detail
        try:
            self.on, self.detail = self._apply(on)
        except Exception as exc:  # noqa: BLE001 - a power setting must never end a lesson
            self.detail = f"could not change: {type(exc).__name__}: {exc}"
        return self.detail

    def _apply(self, on: bool) -> tuple[bool, str]:
        if self.platform == "win32":
            fn = self._execution_state or self._windows_call()
            fn(ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS)
            return on, ("sleep blocked while the tutor runs (SetThreadExecutionState)" if on
                        else "sleep allowed")
        if not on:
            if self._proc is not None:
                self._proc.terminate()
                self._proc = None
            return False, "sleep allowed"
        cmd = self._command()
        if cmd is None:
            return False, f"not available on {self.platform}"
        if self._which(cmd[0]) is None:
            return False, f"{cmd[0]} is not installed - sleep is not blocked"
        self._proc = self._popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        return True, f"sleep blocked while the tutor runs ({cmd[0]})"

    def _command(self) -> list[str] | None:
        if self.platform.startswith("linux"):
            return ["systemd-inhibit", "--what=sleep:idle", "--who=atama-AI",
                    "--why=a lesson is running", "--mode=block", "sleep", "infinity"]
        if self.platform == "darwin":
            return ["caffeinate", "-i"]
        return None

    @staticmethod
    def _windows_call() -> Callable[[int], int]:
        import ctypes
        return ctypes.windll.kernel32.SetThreadExecutionState  # type: ignore[attr-defined]
