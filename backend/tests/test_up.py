"""The launcher's early failures (backend/tools/up.py): a docker that is missing or stopped, and
an app port already taken, each one line before anything starts. Hermetic: fakes for `which`
and `run`; the port probe uses a real loopback socket on an ephemeral port."""
from __future__ import annotations

import socket
import subprocess

from backend.tools import up


class _Done:
    def __init__(self, rc: int, stderr: str = ""):
        self.returncode, self.stderr, self.stdout = rc, stderr, ""


def test_docker_missing_names_the_no_docker_escape():
    msg = up.docker_problem(which=lambda n: None, run=lambda *a, **k: _Done(0))
    assert msg and "--no-docker" in msg and "Docker Desktop" in msg


def test_docker_engine_down_is_reported_not_treated_as_success():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return _Done(1, "error during connect: open //./pipe/dockerDesktopLinuxEngine")

    msg = up.docker_problem(which=lambda n: "C:/docker.exe", run=run)
    assert msg and "not running" in msg and "dockerDesktopLinuxEngine" in msg
    assert calls and calls[0][:2] == ["C:/docker.exe", "info"]


def test_docker_info_that_cannot_run_is_reported():
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 30)

    msg = up.docker_problem(which=lambda n: "docker", run=run)
    assert msg and "TimeoutExpired" in msg


def test_docker_running_is_no_problem():
    assert up.docker_problem(which=lambda n: "docker", run=lambda *a, **k: _Done(0)) is None


def test_port_probe_sees_a_bound_port_and_a_free_one():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        assert up.port_free("127.0.0.1", port) is False
    assert up.port_free("127.0.0.1", port) is True
