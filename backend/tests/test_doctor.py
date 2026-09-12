"""`make doctor` (spec §12 M0): every check's PASS / WARN / FAIL branch, hermetically.

No network, no subprocess, no GPU: each check takes its effects as arguments, and these tests
hand in fakes. The real defaults are exercised once, live, by running the doctor itself.
"""
from __future__ import annotations

import json
from pathlib import Path

from backend import config, constants
from backend.tools import doctor as d
from backend.tools import hooks

# Deliberately not token-shaped (no UUID, no sk-ant-), so `make check-secrets` stays quiet.
WK = "wk-fake-secret-abcdefgh"
BP = "bp-fake-secret-ijklmnop"


def cfg(tmp_path, **env) -> config.Config:
    return config.load(tmp_path / "settings.json", env=env)


def runner(table: dict[str, tuple[int, str, str]], default=(0, "", "")):
    """A `run` fake keyed on the first argv word that matches; records every call."""
    calls: list[dict] = []

    def run(argv, **kw):
        calls.append({"argv": list(argv), **kw})
        for key, result in table.items():
            if any(key in str(a) for a in argv):
                return result
        return default

    run.calls = calls  # type: ignore[attr-defined]
    return run


# ------------------------------------------------------------------ claude CLI
def test_claude_missing_from_path_fails():
    r = d.check_claude_cli(which=lambda name: None, run=runner({}))
    assert r.level == d.FAIL and "not on PATH" in r.message


def test_claude_version_mismatch_warns_and_names_the_pinned_one():
    run = runner({"--version": (0, "9.9.9 (Claude Code)\n", "")})
    r = d.check_claude_cli(which=lambda name: "/bin/claude", run=run)
    assert r.level == d.WARN and "9.9.9" in r.message and constants.CLAUDE_CLI_VERSION_VERIFIED in r.message


def test_claude_verified_version_passes():
    run = runner({"--version": (0, f"{constants.CLAUDE_CLI_VERSION_VERIFIED} (Claude Code)\n", "")})
    assert d.check_claude_cli(which=lambda name: "/bin/claude", run=run).level == d.PASS


def test_claude_version_command_failure_fails():
    run = runner({"--version": (1, "", "boom")})
    r = d.check_claude_cli(which=lambda name: "/bin/claude", run=run)
    assert r.level == d.FAIL and "boom" in r.message


# ------------------------------------------------------------- ANTHROPIC_API_KEY
def test_api_key_present_is_a_hard_fail_even_when_empty():
    assert d.check_api_key_absent(environ={"ANTHROPIC_API_KEY": ""}).level == d.FAIL
    assert d.check_api_key_absent(environ={"PATH": "x"}).level == d.PASS


# ---------------------------------------------------------------- claude probe
def transcript(source="none", reply="OK", init=True):
    lines = []
    if init:
        lines.append(json.dumps({"type": "system", "subtype": "init", "apiKeySource": source}))
    lines.append("not json")
    lines.append(json.dumps({"type": "result", "subtype": "success", "result": reply}))
    return "\n".join(lines) + "\n"


def test_parse_probe_stream_reads_init_and_result():
    assert d.parse_probe_stream(transcript()) == ("none", "OK")
    assert d.parse_probe_stream(transcript(init=False)) == (None, "OK")
    assert d.parse_probe_stream("") == (None, None)


def test_probe_passes_on_subscription_auth_and_ok(tmp_path):
    run = runner({"claude": (0, transcript(), "")})
    r = d.check_claude_probe(cfg(tmp_path), which=lambda n: "/bin/claude", run=run,
                             child_env=lambda: {"PATH": "/bin"}, cwd=tmp_path / "cwd")
    assert r.level == d.PASS, r
    call = run.calls[0]
    assert list(d.PROBE_ARGS) == call["argv"][1:1 + len(d.PROBE_ARGS)]
    assert "--model" in call["argv"]
    assert call["env"] == {"PATH": "/bin"}                # the allowlisted env, nothing else
    assert call["cwd"] == str(tmp_path / "cwd")


def test_probe_fails_on_api_billing(tmp_path):
    run = runner({"claude": (0, transcript(source="ANTHROPIC_API_KEY"), "")})
    r = d.check_claude_probe(cfg(tmp_path), which=lambda n: "/bin/claude", run=run,
                             child_env=lambda: {}, cwd=tmp_path / "cwd")
    assert r.level == d.FAIL and "ANTHROPIC_API_KEY" in r.message and "ADR-001" in r.message


def test_probe_fails_without_init_and_shows_stderr_tail(tmp_path):
    run = runner({"claude": (1, "", "Not logged in\nPlease run /login")})
    r = d.check_claude_probe(cfg(tmp_path), which=lambda n: "/bin/claude", run=run,
                             child_env=lambda: {}, cwd=tmp_path / "cwd")
    assert r.level == d.FAIL and "/login" in r.message


def test_probe_warns_when_auth_is_fine_but_the_reply_is_not_ok(tmp_path):
    run = runner({"claude": (0, transcript(reply="Sure, OK!"), "")})
    r = d.check_claude_probe(cfg(tmp_path), which=lambda n: "/bin/claude", run=run,
                             child_env=lambda: {}, cwd=tmp_path / "cwd")
    assert r.level == d.WARN and "Sure, OK!" in r.message


def test_probe_accepts_ok_with_trailing_punctuation(tmp_path):
    run = runner({"claude": (0, transcript(reply="OK."), "")})
    r = d.check_claude_probe(cfg(tmp_path), which=lambda n: "/bin/claude", run=run,
                             child_env=lambda: {}, cwd=tmp_path / "cwd")
    assert r.level == d.PASS


def test_probe_refuses_a_cwd_inside_the_repo(tmp_path):
    run = runner({})
    r = d.check_claude_probe(cfg(tmp_path), which=lambda n: "/bin/claude", run=run,
                             child_env=lambda: {}, cwd=config.REPO_ROOT / ".cache" / "x")
    assert r.level == d.FAIL and "CLAUDE.md" in r.message and not run.calls


def test_probe_fails_when_claude_is_off_the_allowlisted_path(tmp_path):
    r = d.check_claude_probe(cfg(tmp_path), which=lambda n: None, run=runner({}),
                             child_env=lambda: {}, cwd=tmp_path / "cwd")
    assert r.level == d.FAIL


# -------------------------------------------------------------- loopback only
def test_loopback_service_up_on_loopback_only_passes():
    r = d.check_loopback_service("VOICEVOX", "http://127.0.0.1:50021", probe_path="/version",
                                 http_get=lambda url, timeout=2.0: (200, '"0.25.2"'),
                                 ipv4s=lambda: ["192.168.1.20"], tcp=lambda h, p, timeout=0.5: False)
    assert r.level == d.PASS and "0.25.2" in r.message


def test_loopback_service_reachable_on_the_lan_fails():
    r = d.check_loopback_service("VOICEVOX", "http://127.0.0.1:50021", probe_path="/version",
                                 http_get=lambda url, timeout=2.0: (200, ""),
                                 ipv4s=lambda: ["192.168.1.20", "10.0.0.5"],
                                 tcp=lambda h, p, timeout=0.5: h == "10.0.0.5")
    assert r.level == d.FAIL and "10.0.0.5:50021" in r.message and "192.168.1.20" not in r.message


def test_loopback_service_down_is_a_warning_or_normal():
    kw = dict(http_get=lambda url, timeout=2.0: None, ipv4s=lambda: ["192.168.1.20"],
              tcp=lambda h, p, timeout=0.5: False)
    assert d.check_loopback_service("VOICEVOX", "http://127.0.0.1:50021", probe_path="/version", **kw).level == d.WARN
    assert d.check_loopback_service("app port", "http://127.0.0.1:8000", down=d.PASS, **kw).level == d.PASS
    assert d.check_loopback_service("SearXNG", "http://127.0.0.1:8888", down=d.FAIL, **kw).level == d.FAIL


def test_loopback_service_tcp_probe_when_no_http_path():
    seen = []

    def tcp(h, p, timeout=0.5):
        seen.append((h, p))
        return h == "127.0.0.1"

    r = d.check_loopback_service("app port", "http://127.0.0.1:8000", ipv4s=lambda: ["192.168.1.20"], tcp=tcp)
    assert r.level == d.PASS and ("127.0.0.1", 8000) in seen and ("192.168.1.20", 8000) in seen


def test_loopback_service_configured_off_loopback_fails_before_probing():
    r = d.check_loopback_service("app port", "http://0.0.0.0:8000",
                                 http_get=lambda *a, **k: (200, ""), ipv4s=lambda: [], tcp=lambda *a, **k: True)
    assert r.level == d.FAIL and "ADR-017" in r.message


# --------------------------------------------------------------------- docker
def test_parse_compose_ps_accepts_array_ndjson_and_nothing():
    assert d.parse_compose_ps("") == []
    assert d.parse_compose_ps('[{"Service": "voicevox", "State": "running"}]') == [{"Service": "voicevox", "State": "running"}]
    rows = d.parse_compose_ps('{"Service": "voicevox", "State": "running"}\n{"Service": "searxng", "State": "exited"}\n')
    assert [r["Service"] for r in rows] == ["voicevox", "searxng"]


def test_docker_absent_warns():
    r = d.check_docker(which=lambda n: None, run=runner({}))
    assert r.level == d.WARN and "Docker Desktop" in r.message


def test_docker_engine_down_warns():
    run = runner({"info": (1, "", "error during connect")})
    r = d.check_docker(which=lambda n: "docker", run=run)
    assert r.level == d.WARN and "not running" in r.message


def test_docker_compose_states(tmp_path):
    ps = '{"Service": "voicevox", "State": "running"}\n{"Service": "searxng", "State": "running"}\n'
    run = runner({"info": (0, "29.7.2\n", ""), "compose": (0, ps, "")})
    r = d.check_docker(which=lambda n: "docker", run=run, root=tmp_path, environ={"PATH": "x"})
    assert r.level == d.PASS and "voicevox running" in r.message and "29.7.2" in r.message
    compose_call = next(c for c in run.calls if "compose" in c["argv"])
    assert compose_call["env"]["SEARXNG_SECRET"]           # compose needs it just to list
    assert compose_call["cwd"] == str(tmp_path)

    run = runner({"info": (0, "29.7.2\n", ""), "compose": (0, '{"Service": "voicevox", "State": "exited"}', "")})
    assert d.check_docker(which=lambda n: "docker", run=run, root=tmp_path, environ={}).level == d.WARN

    run = runner({"info": (0, "29.7.2\n", ""), "compose": (0, "", "")})
    r = d.check_docker(which=lambda n: "docker", run=run, root=tmp_path, environ={})
    assert r.level == d.WARN and "no atama-AI containers" in r.message

    run = runner({"info": (0, "29.7.2\n", ""), "compose": (1, "", "interpolation error")})
    assert d.check_docker(which=lambda n: "docker", run=run, root=tmp_path, environ={}).level == d.WARN


# --------------------------------------------------------------------- tokens
def test_tokens_report_set_or_unset_without_the_value(tmp_path):
    results = {r.name: r for r in d.check_tokens(cfg(tmp_path, WANIKANI_TOKEN=WK))}
    assert results["WANIKANI_TOKEN"].level == d.PASS
    assert WK not in results["WANIKANI_TOKEN"].message and WK[-4:] in results["WANIKANI_TOKEN"].message
    assert results["BUNPRO_API_TOKEN"].level == d.WARN and "Account" in results["BUNPRO_API_TOKEN"].message
    assert results["CLAUDE_CODE_OAUTH_TOKEN"].level == d.PASS and "interactive" in results["CLAUDE_CODE_OAUTH_TOKEN"].message


def test_wanikani_scope_line_instructs_and_names_every_write_scope():
    r = d.check_wanikani_scopes()
    assert r.level == d.PASS and "cannot be verified" in r.message
    for scope in d.WANIKANI_WRITE_SCOPES:
        assert scope in r.message


# ------------------------------------------------------------------- srs live
class _Client:
    def __init__(self, payload=None, error: Exception | None = None):
        self.payload, self.error, self.paths = payload, error, []

    def __call__(self, service, token):
        self.service, self.token = service, token
        return self

    def get(self, path):
        self.paths.append(path)
        if self.error:
            raise self.error
        return self.payload


def test_srs_live_without_a_token_warns_and_makes_no_request():
    client = _Client()
    r = d.check_srs_live("wanikani", "", client_factory=client)
    assert r.level == d.WARN and client.paths == []


def test_srs_live_makes_exactly_one_get_per_service():
    client = _Client(payload={"data": {"level": 12}})
    r = d.check_srs_live("wanikani", WK, client_factory=client)
    assert r.level == d.PASS and client.paths == ["/v2/user"] and "level 12" in r.message

    client = _Client(payload={})
    r = d.check_srs_live("bunpro", BP, client_factory=client)
    assert r.level == d.PASS and client.paths == [constants.BUNPRO_API_PREFIX + "/user"]


def test_srs_live_failure_is_reported_without_the_token():
    from backend.srs.http import SrsError

    client = _Client(error=SrsError("wanikani: HTTP 401 for /v2/user: unauthorized", 401))
    r = d.check_srs_live("wanikani", WK, client_factory=client)
    assert r.level == d.FAIL and "401" in r.message and WK not in r.message

    client = _Client(error=RuntimeError(f"leaked {WK}"))   # not sanitised: only the type is shown
    r = d.check_srs_live("wanikani", WK, client_factory=client)
    assert r.level == d.FAIL and WK not in r.message


# ------------------------------------------------------------------ gitignore
def test_gitignore_all_ignored_passes(tmp_path):
    run = runner({}, default=(0, "", ""))
    r = d.check_gitignore(run=run, root=tmp_path)
    assert r.level == d.PASS and len(run.calls) == len(d.IGNORED_PATHS)
    assert all(c["argv"][:3] == ["git", "check-ignore", "-q"] for c in run.calls)


def test_gitignore_missing_rule_fails_and_names_the_path(tmp_path):
    run = runner({".env": (1, "", "")}, default=(0, "", ""))
    r = d.check_gitignore(run=run, root=tmp_path)
    assert r.level == d.FAIL and ".env" in r.message and "logs/" not in r.message


def test_gitignore_without_git_warns(tmp_path):
    assert d.check_gitignore(run=runner({}, default=(127, "", "")), root=tmp_path).level == d.WARN


# ----------------------------------------------------------------------- hook
def test_hook_missing_stale_and_current(tmp_path):
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    assert d.check_hook(root=tmp_path).level == d.WARN
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    r = d.check_hook(root=tmp_path)
    assert r.level == d.WARN and "differs" in r.message
    hook.write_bytes(hooks.HOOK.replace("\n", "\r\n").encode("utf-8"))   # CRLF from a Windows editor is fine
    assert d.check_hook(root=tmp_path).level == d.PASS


# ----------------------------------------------------------------------- cuda
def test_cuda_missing_over_and_fine():
    from backend.vram import Reading

    assert d.check_cuda(10, read=lambda: None).level == d.WARN
    over = d.check_cuda(10, read=lambda: Reading(12 * 1024, 16 * 1024))
    assert over.level == d.WARN and "12.0" in over.message
    ok = d.check_cuda(10, read=lambda: Reading(1500, 16 * 1024))
    assert ok.level == d.PASS and "1.5" in ok.message


# --------------------------------------------------------------------- avatar
def test_avatar_present_missing_and_undeclared(tmp_path):
    glb = tmp_path / "minami.glb"
    personas = lambda: [("minami", glb), ("mori", tmp_path / "mori.glb")]  # noqa: E731
    assert d.check_avatar(cfg(tmp_path, TUTOR_PERSONA="minami"), personas=personas).level == d.WARN
    glb.write_bytes(b"glTF")
    assert d.check_avatar(cfg(tmp_path, TUTOR_PERSONA="minami"), personas=personas).level == d.PASS
    r = d.check_avatar(cfg(tmp_path, TUTOR_PERSONA="nobody"), personas=personas)
    assert r.level == d.WARN and "nobody" in r.message


# --------------------------------------------------------------- status table
def test_status_table_from_snapshot_or_none(tmp_path):
    assert d.status_table(tmp_path / "missing.json") is None
    snap = tmp_path / "status.json"
    snap.write_text(json.dumps({
        "wanikani": {"state": "ok", "detail": "level 4", "last_error": ""},
        "bunpro": {"state": "not-a-state", "detail": "x"},          # skipped, never raises
        "junk": 3,
    }), encoding="utf-8")
    table = d.status_table(snap)
    assert table and "wanikani     ok             level 4" in table and "bunpro       (no report)" in table
    snap.write_text("{not json", encoding="utf-8")
    assert d.status_table(snap) is None


# ------------------------------------------------------------------- topology
def test_topology_detection(tmp_path):
    assert "Windows" in d.detect_topology(platform="win32").message
    wsl = d.detect_topology(platform="linux", proc_version="Linux version 5.15 (Microsoft@Microsoft.com)", repo=Path("/home/u/atama"))
    assert wsl.level == d.PASS and "WSL2" in wsl.message
    mnt = d.detect_topology(platform="linux", proc_version="microsoft-standard-WSL2", repo=Path("/mnt/c/Atama"))
    assert mnt.level == d.WARN and "/mnt" in mnt.message
    assert "Linux" in d.detect_topology(platform="linux", proc_version="Linux version 6.1 (gcc)").message


# --------------------------------------------------------------------- report
def test_report_exit_code_follows_fail_only():
    lines = []
    ok = [d.Result(d.PASS, "a", "fine"), d.Result(d.WARN, "b", "hmm")]
    assert d.report(ok, None, out=lines.append) == 0
    assert any("1 warnings" in l for l in lines) and any("no snapshot" in l for l in lines)
    lines.clear()
    assert d.report([*ok, d.Result(d.FAIL, "c", "bad")], "TABLE", out=lines.append) == 1
    assert "TABLE" in lines and any(l.startswith("FAIL  c") for l in lines)


def test_collect_runs_every_check_in_spec_order(tmp_path, monkeypatch):
    """The wiring: each check is called once, the SRS live pair only with --live."""
    called = []

    def fake(name, level=d.PASS):
        def _f(*a, **k):
            called.append(name)
            return d.Result(level, name, "")
        return _f

    for fn in ("check_claude_cli", "check_api_key_absent", "check_claude_probe", "check_loopback_service",
               "check_docker", "check_wanikani_scopes", "check_srs_live", "check_gitignore", "check_hook",
               "check_cuda", "check_avatar", "detect_topology"):
        monkeypatch.setattr(d, fn, fake(fn))
    monkeypatch.setattr(d, "check_tokens", lambda c: [d.Result(d.PASS, "tokens", "")])
    monkeypatch.setattr(d, "_proc_version", lambda: "")
    c = cfg(tmp_path)
    results = d.collect(c, live=False)
    assert "check_srs_live" not in called and called[0] == "check_claude_cli" and called[1] == "check_api_key_absent"
    assert called.count("check_loopback_service") == 3
    called.clear()
    d.collect(c, live=True, skip_probe=True)
    assert called.count("check_srs_live") == 2 and "check_claude_probe" not in called
    assert all(isinstance(r, d.Result) for r in results)
