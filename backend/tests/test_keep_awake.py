"""Keeping the computer awake (ADR-041): the OS call on Windows, a held inhibitor elsewhere, and
never an exception out of a power setting."""
from __future__ import annotations

from backend.keep_awake import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake


def test_windows_sets_and_clears_the_execution_state():
    calls: list[int] = []
    k = KeepAwake(platform="win32", execution_state=lambda flags: calls.append(flags) or 1)
    assert "blocked" in k.set(True) and k.on
    assert k.set(True) == k.detail and len(calls) == 1          # idempotent
    assert "allowed" in k.set(False) and not k.on
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS]


class FakeProc:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True


def test_linux_holds_a_systemd_inhibitor_and_releases_it():
    started: list[list[str]] = []
    procs: list[FakeProc] = []

    def popen(cmd, **kw):
        started.append(cmd)
        procs.append(FakeProc())
        return procs[-1]

    k = KeepAwake(platform="linux", popen=popen, which=lambda name: "/usr/bin/" + name)
    assert "systemd-inhibit" in k.set(True) and k.on
    assert started[0][:2] == ["systemd-inhibit", "--what=sleep:idle"]
    k.set(False)
    assert procs[0].terminated and not k.on


def test_a_missing_tool_is_reported_not_raised():
    k = KeepAwake(platform="linux", popen=lambda *a, **k: FakeProc(), which=lambda name: None)
    assert "not installed" in k.set(True) and not k.on
    k = KeepAwake(platform="freebsd14", popen=lambda *a, **k: FakeProc())
    assert "not available" in k.set(True) and not k.on


def test_an_os_error_never_escapes():
    def boom(flags):
        raise OSError("no kernel32 here")

    k = KeepAwake(platform="win32", execution_state=boom)
    assert "could not change" in k.set(True) and not k.on
