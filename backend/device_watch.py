"""Keep the audio device list current while the app runs (spec §9).

PortAudio sees only the devices that existed when it initialised, and re-initialising it inside
the app would tear down the microphone stream that stays open for the whole session. So the list
is watched from a CHILD process, which opens no stream and can re-initialise freely:

    python -m backend.device_watch     # one JSON line per change: {"input": [...], "output": [...]}

`DeviceWatch` runs that child from the app and calls back on every change: the settings panel's
pickers refresh, and a chosen microphone that comes back is switched to.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any, Callable

from backend import config

#: Plugging a headset in should show up about as fast as the student looks back at the screen.
INTERVAL_S = 2.0


def _child() -> int:
    from backend import audio, settings_view

    last = None
    while True:
        audio.rescan()                   # always allowed here: this process never opens a stream
        now = settings_view.device_options(fresh=True)
        if now != last:
            try:
                print(json.dumps(now), flush=True)   # ASCII escapes: safe on any console codepage
            except (BrokenPipeError, OSError):
                return 0                 # the app has gone, so do we
            last = now
        time.sleep(INTERVAL_S)


class DeviceWatch:
    """Runs `_child` as a subprocess and calls `on_change(options)` for each new list."""

    def __init__(self, on_change: Callable[[dict[str, Any]], None]) -> None:
        self.on_change = on_change
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> bool:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "backend.device_watch",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                cwd=str(config.REPO_ROOT))
        except (OSError, NotImplementedError):
            # No watcher: recovery still works (the mic thread re-scans on its own), the panel's
            # device list just refreshes less often.
            return False
        self._task = asyncio.create_task(self._read())
        return True

    async def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return
            try:
                self.on_change(json.loads(line))
            except Exception:  # noqa: BLE001 - a callback error must not end the watch
                pass

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 2)
            except Exception:  # noqa: BLE001 - it is a child we are abandoning either way
                pass


if __name__ == "__main__":
    try:
        sys.exit(_child())
    except KeyboardInterrupt:
        sys.exit(0)
