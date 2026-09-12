"""`make doctor`: the environment pre-flight (spec §4b, §5, §5b, §11, §12 M0, §15).

    python -m backend.tools.doctor            # every check; no SRS request is made
    python -m backend.tools.doctor --live     # plus ONE GET per configured SRS token to prove it
                                              # authenticates (ADR-024: SRS calls only on demand)

Every check prints one line — PASS, WARN or FAIL — with what to do about it. The exit code is
non-zero when anything FAILed. The read-only gate (spec §0) runs first, from the Makefile target.

Each check is a small function whose side effects (subprocesses, sockets, files) come in through
arguments with real defaults, so the tests cover every branch without a network, a GPU or a
`claude` on PATH. Nothing here prints a secret: tokens are reported as set/unset, and errors from
the SRS client arrive sanitised.

This is the one code file allowed to hold the WaniKani write-scope names (spec §0 rule 6): the
doctor *instructs* the user to leave them unticked. It cannot verify them — ROADMAP V0.9
(2026-09-09) established that `/v2/user` does not expose the token's scopes, and probing them
would need a write, which this project never makes.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from backend import config, constants, vram
from backend.status import SERVICES, STATES, StatusRegistry
from backend.tools import hooks

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

#: The WaniKani token permissions that must stay UNTICKED at token creation. Printed, never
#: probed (see the module docstring). Rule 6 allows these strings in this file only.
WANIKANI_WRITE_SCOPES = ("assignments:start", "reviews:create", "study_materials:create",
                         "study_materials:update", "user:update")

#: Verified 2026-09-12 against `claude --help` (2.1.159): `-p, --print` takes the prompt as its
#: positional argument; `--output-format stream-json` and `--tools ""` only work with --print;
#: `--strict-mcp-config` limits MCP servers to --mcp-config (none given here, so none). The
#: flags are the persistent subprocess's base set (constants.CLAUDE_BASE_ARGS) minus the two that
#: only make sense for a long-lived stdin stream (--input-format stream-json,
#: --include-partial-messages). The model is the configured fallback (haiku by default): the
#: answer is one word and the point is the auth assertion, not the model.
PROBE_PROMPT = "respond with OK"
PROBE_ARGS = ("-p", PROBE_PROMPT, "--output-format", "stream-json", "--verbose",
              "--strict-mcp-config", "--tools", "")
PROBE_TIMEOUT_S = 120.0
PROBE_REPLY = "OK"

#: Spec §11: every one of these must be ignored (`git check-ignore`), each named for its rule.
IGNORED_PATHS = (
    ".env", "mcp.json", "settings.json", "logs/", ".cache/",
    "prompts/x_rendered.md", "frontend/public/x.glb", "backend/tests/fixtures/private/",
)

Runner = Callable[..., tuple[int, str, str]]


@dataclass(frozen=True)
class Result:
    level: str
    name: str
    message: str

    def line(self) -> str:
        return f"{self.level:<4}  {self.name:<24} {self.message}"


# ----------------------------------------------------------------- default effects
def run_cmd(argv: list[str], *, env: Mapping[str, str] | None = None, cwd: str | None = None,
            timeout: float = 30.0) -> tuple[int, str, str]:
    """(returncode, stdout, stderr). A missing program reads as rc 127, a timeout as 124."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=dict(env) if env is not None else None, cwd=cwd, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: no answer within {timeout:.0f}s"
    except OSError as e:
        return 126, "", f"{argv[0]}: {e}"
    return done.returncode, done.stdout or "", done.stderr or ""


def http_get(url: str, timeout: float = 2.0) -> tuple[int, str] | None:
    """(status, body) or None when nothing answers. Plain GET, redirects not followed."""
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # pragma: no cover - never expected locally
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status, resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError, ValueError):
        return None


def local_ipv4s() -> list[str]:
    """Every non-loopback IPv4 address this host answers on."""
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except (socket.gaierror, OSError):
        pass
    # The address of the default route, without sending anything (a UDP connect only picks it).
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1 (RFC 5737): never routed, never contacted
            found.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    return sorted(ip for ip in found if not ip.startswith("127.") and ip != "0.0.0.0")


def tcp_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ------------------------------------------------------------------------- checks
def check_claude_cli(*, which=shutil.which, run: Runner = run_cmd) -> Result:
    exe = which("claude")
    if not exe:
        return Result(FAIL, "claude CLI", "`claude` is not on PATH. Install Claude Code, then run `claude` and "
                                         "/login (README: Claude login).")
    rc, out, err = run([exe, "--version"], timeout=20)
    if rc != 0:
        return Result(FAIL, "claude CLI", f"`claude --version` failed (rc={rc}): {(err or out).strip()[:120]}")
    version = (out.strip().split() or [""])[0]
    pinned = constants.CLAUDE_CLI_VERSION_VERIFIED
    if version != pinned:
        return Result(WARN, "claude CLI", f"{version} on PATH; the flags were verified against {pinned} (ADR-015). "
                                         "Re-verify `claude --help` and update backend/constants.py.")
    return Result(PASS, "claude CLI", f"{version} (the verified version)")


def check_api_key_absent(*, environ: Mapping[str, str] | None = None) -> Result:
    environ = os.environ if environ is None else environ
    if "ANTHROPIC_API_KEY" in environ:
        return Result(FAIL, "ANTHROPIC_API_KEY", "set in this shell. Unset it: it overrides subscription auth, "
                                                "bills the API, and would leak into every subprocess (spec §4b).")
    return Result(PASS, "ANTHROPIC_API_KEY", "absent from the shell")


def parse_probe_stream(stdout: str) -> tuple[str | None, str | None]:
    """(init.apiKeySource, result.result) from a stream-json transcript; None where missing."""
    source = reply = None
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if (ev.get("type"), ev.get("subtype")) == constants.CLAUDE_EVENT_INIT:
            source = ev.get("apiKeySource")
        elif ev.get("type") == constants.CLAUDE_EVENT_RESULT and isinstance(ev.get("result"), str):
            reply = ev["result"]
    return source, reply


def check_claude_probe(cfg, *, which=None, run: Runner = run_cmd,
                       child_env: Callable[[], dict[str, str]] | None = None,
                       cwd: Path | None = None) -> Result:
    """One trivial `claude -p` under the app's own spawn rules — allowlisted env, cwd outside the
    repo, no shell — asserting subscription auth from `init.apiKeySource` (spec §4b)."""
    if child_env is None:
        from backend.brain.claude_cli import child_env as _child_env  # import has no side effects
        child_env = _child_env
    env = child_env()
    exe = shutil.which("claude", path=env.get("PATH")) if which is None else which("claude")
    if not exe:
        return Result(FAIL, "claude probe", "`claude` is not on the allowlisted PATH (spec §4).")
    cwd = config.claude_cwd(cfg) if cwd is None else cwd
    if cwd == config.REPO_ROOT or config.REPO_ROOT in cwd.parents:
        return Result(FAIL, "claude probe", f"claude cwd {cwd} is inside the repo; it would inherit CLAUDE.md (spec §4).")
    try:
        cwd.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return Result(FAIL, "claude probe", f"cannot create the claude cwd {cwd}: {e}")
    model = str(getattr(cfg, "CLAUDE_FALLBACK_MODEL", "") or "haiku")
    rc, out, err = run([exe, *PROBE_ARGS, "--model", model], env=env, cwd=str(cwd), timeout=PROBE_TIMEOUT_S)
    source, reply = parse_probe_stream(out)
    if source is None:
        tail = (err.strip().splitlines() or [""])[-1][:160]
        return Result(FAIL, "claude probe", f"no `init` event from `claude -p` (rc={rc}). Not logged in? Run `claude`, "
                                            f"then /login (README: Claude login). stderr: {tail or '(silent)'}")
    if source != constants.CLAUDE_INIT_APIKEYSOURCE_SUBSCRIPTION:
        return Result(FAIL, "claude probe", f"init.apiKeySource={source!r}, expected 'none': an API key reaches the "
                                            "child from somewhere (a profile or settings file). The tutor refuses "
                                            "to run on API billing (ADR-001).")
    if reply is None or reply.strip().strip(".!。").upper() != PROBE_REPLY:
        shown = "(no result)" if reply is None else reply.strip()[:60]
        return Result(WARN, "claude probe", f"subscription auth OK (apiKeySource=none) but the reply was {shown!r}, "
                                            f"not {PROBE_REPLY!r} (rc={rc}).")
    return Result(PASS, "claude probe", f"apiKeySource=none, replied {PROBE_REPLY} ({model})")


def check_loopback_service(name: str, url: str, *, probe_path: str = "", down: str = WARN,
                           http_get=http_get, ipv4s=local_ipv4s, tcp=tcp_reachable) -> Result:
    """Reachable on 127.0.0.1 — and on NO other interface (ADR-017). A service that is down is
    reported at level `down` (a container that is not started is a warning; the app itself not
    running is normal before `make run`); one that answers on the LAN is always a failure."""
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if host not in ("127.0.0.1", "localhost", "::1"):
        return Result(FAIL, name, f"configured at {host}:{port}, not loopback (ADR-017). Fix it in settings → Advanced.")
    answer = http_get(f"{parts.scheme or 'http'}://{host}:{port}{probe_path}") if probe_path else None
    up = answer is not None if probe_path else tcp(host, port)
    if not up:
        if down == PASS:
            return Result(PASS, name, f"nothing listening on {host}:{port} (not running; LAN exposure is checked "
                                      "once it is)")
        return Result(down, name, f"not answering on {host}:{port}. Start it (`make run` brings the containers "
                                  "up), then run the doctor again.")
    exposed = [ip for ip in ipv4s() if tcp(ip, port)]
    if exposed:
        where = ", ".join(f"{ip}:{port}" for ip in exposed)
        return Result(FAIL, name, f"also reachable on {where} — it must bind loopback only (ADR-017): "
                                  f"`127.0.0.1:{port}:...` in docker-compose.yml, HOST=127.0.0.1 for the app.")
    detail = ""
    if probe_path and answer:
        status, body = answer
        detail = f", HTTP {status}" + (f" {body.strip()[:40]}" if status == 200 and body.strip() else "")
    return Result(PASS, name, f"loopback only on {host}:{port}{detail}")


def parse_compose_ps(stdout: str) -> list[dict]:
    """`docker compose ps --format json` is NDJSON on Compose >= 2.21 and a JSON array before."""
    text = stdout.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else [parsed]
    except ValueError:
        rows = []
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows


def check_docker(*, which=shutil.which, run: Runner = run_cmd, root: Path | None = None,
                 environ: Mapping[str, str] | None = None) -> Result:
    root = config.REPO_ROOT if root is None else root
    environ = os.environ if environ is None else environ
    docker = which("docker")
    if not docker:
        return Result(WARN, "docker", "not on PATH. VOICEVOX and SearXNG run as containers: install Docker Desktop, "
                                     "or run them yourself and start with `--no-docker`.")
    rc, out, err = run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=20)
    if rc != 0:
        return Result(WARN, "docker", "installed but the engine is not running (`docker info` failed). Start Docker "
                                     "Desktop, then `make run`.")
    engine = out.strip() or "?"
    # compose refuses even to list services while SEARXNG_SECRET is unset (it interpolates the
    # file). Listing starts nothing, so any value will do, and none is stored.
    env = {**environ, "SEARXNG_SECRET": environ.get("SEARXNG_SECRET") or "doctor-lists-only"}
    rc, out, err = run([docker, "compose", "ps", "--all", "--format", "json"], env=env, cwd=str(root), timeout=30)
    if rc != 0:
        return Result(WARN, "docker", f"engine {engine}; `docker compose ps` failed: {(err or out).strip()[:120]}")
    rows = parse_compose_ps(out)
    if not rows:
        return Result(WARN, "docker", f"engine {engine}; no atama-AI containers yet. `make run` creates and starts them.")
    states = {str(r.get("Service") or r.get("Name") or "?"): str(r.get("State") or "?") for r in rows}
    summary = ", ".join(f"{svc} {st}" for svc, st in sorted(states.items()))
    if all(st == "running" for st in states.values()):
        return Result(PASS, "docker", f"engine {engine}; {summary}")
    return Result(WARN, "docker", f"engine {engine}; {summary}. `make run` starts what is stopped.")


def check_tokens(cfg) -> list[Result]:
    """Set / unset per secret key of the schema. Values are never printed (ADR-022)."""
    view = cfg.public_view()
    out = []
    for key in ("WANIKANI_TOKEN", "BUNPRO_API_TOKEN"):
        if view[key]["set"]:
            out.append(Result(PASS, key, f"set ({view[key]['hint']})"))
        else:
            out.append(Result(WARN, key, "not set: the tutor teaches as if you were an early beginner. Add it on the "
                                         "settings page → Account (spec §11)."))
    oauth = view["CLAUDE_CODE_OAUTH_TOKEN"]
    out.append(Result(PASS, "CLAUDE_CODE_OAUTH_TOKEN", f"set ({oauth['hint']})" if oauth["set"]
                      else "not set: the interactive `claude` login is used (README: Claude login)"))
    return out


def check_wanikani_scopes() -> Result:
    """The token's permissions cannot be read back (V0.9): say what they must be."""
    return Result(PASS, "WaniKani scopes", "cannot be verified from the API (V0.9): create the token with every "
                                           f"write permission unticked — {', '.join(WANIKANI_WRITE_SCOPES)}.")


def check_srs_live(service: str, token: str, *, client_factory=None) -> Result:
    """ONE GET to prove the token authenticates. Only with --live (ADR-024)."""
    name = f"{service} live"
    if not token:
        return Result(WARN, name, "no token configured; nothing to check")
    if client_factory is None:
        from backend.srs.http import SrsClient
        client_factory = SrsClient
    path = "/v2/user" if service == "wanikani" else constants.BUNPRO_API_PREFIX + constants.BUNPRO_READ_ENDPOINTS["user"]
    try:
        payload = client_factory(service, token).get(path)
    except Exception as e:  # SrsError arrives sanitised; anything else is reported by type only
        detail = str(e)[:160] if type(e).__name__ == "SrsError" else ""
        return Result(FAIL, name, f"GET {path} failed: {type(e).__name__} {detail}".rstrip())
    detail = ""
    if service == "wanikani" and isinstance(payload, dict):
        data = payload.get("data") or {}
        if isinstance(data, dict) and data.get("level"):
            detail = f", level {data.get('level')}"
    return Result(PASS, name, f"GET {path} authenticated{detail}")


def check_gitignore(*, run: Runner = run_cmd, root: Path | None = None) -> Result:
    root = config.REPO_ROOT if root is None else root
    missing = []
    for rel in IGNORED_PATHS:
        rc, _, _ = run(["git", "check-ignore", "-q", rel], cwd=str(root), timeout=20)
        if rc == 127:
            return Result(WARN, ".gitignore", "git is not on PATH; cannot verify the ignored paths (spec §11)")
        if rc != 0:
            missing.append(rel)
    if missing:
        return Result(FAIL, ".gitignore", f"not ignored: {', '.join(missing)}. Restore the rules in .gitignore "
                                         "(spec §11) before anything personal lands in a commit.")
    return Result(PASS, ".gitignore", f"all {len(IGNORED_PATHS)} personal/generated paths are ignored")


def check_hook(*, root: Path | None = None) -> Result:
    root = config.REPO_ROOT if root is None else root
    path = root / ".git" / "hooks" / "pre-commit"
    if not path.is_file():
        return Result(WARN, "pre-commit hook", "not installed. `make hooks` (python -m backend.tools.hooks) installs "
                                              "the gate + secret scan before every commit (spec §11).")
    try:
        installed = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError):
        return Result(WARN, "pre-commit hook", "present but unreadable; `make hooks` rewrites it.")
    if installed != hooks.HOOK:
        return Result(WARN, "pre-commit hook", "differs from what `make hooks` installs; re-run it.")
    return Result(PASS, "pre-commit hook", "installed and current")


def check_cuda(limit_gb: float, *, read=vram.read) -> Result:
    reading = read()
    if reading is None:
        return Result(WARN, "CUDA", "nvidia-smi not found or no GPU reported: faster-whisper needs CUDA (ADR-004); "
                                   "on WSL2 it must work INSIDE WSL2 (spec §15).")
    if vram.over(reading, limit_gb):
        return Result(WARN, "CUDA", f"{reading.used_gb:.1f} / {reading.total_gb:.1f} GB in use, above the "
                                   f"{limit_gb:g} GB budget (spec §10b). Close whatever else is on the GPU.")
    return Result(PASS, "CUDA", f"nvidia-smi: {reading.used_gb:.1f} / {reading.total_gb:.1f} GB in use "
                               f"(budget {limit_gb:g} GB)")


def check_avatar(cfg, *, personas=None) -> Result:
    """The configured tutor's face is on disk (ROADMAP 21: a fresh clone has none)."""
    if personas is None:
        try:
            from backend.tools import check_avatar as ca
            personas = ca.personas
        except Exception as e:  # pragma: no cover - the persona files are part of the tree
            return Result(WARN, "avatar", f"could not read the personas: {type(e).__name__}: {e}")
    faces = dict(personas())
    persona = str(getattr(cfg, "TUTOR_PERSONA", "") or "")
    glb = faces.get(persona)
    if glb is None:
        return Result(WARN, "avatar", f"persona {persona!r} declares no avatar; the page would show no face")
    if not glb.exists():
        return Result(WARN, "avatar", f"{glb.name} missing for {persona}: `make avatar` fetches the default one")
    return Result(PASS, "avatar", f"{glb.name} ({persona})")


def status_table(snapshot_path: Path) -> str | None:
    """The §5b table from the last persisted registry snapshot, or None when there is none."""
    try:
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    reg = StatusRegistry()
    for service in SERVICES:
        row = raw.get(service) if isinstance(raw, dict) else None
        if not isinstance(row, dict) or row.get("state") not in STATES.get(service, ()):
            continue
        reg.report(service, str(row["state"]), str(row.get("detail") or ""), str(row.get("last_error") or ""))
    return reg.table()


def detect_topology(*, platform: str = sys.platform, proc_version: str = "", repo: Path | None = None) -> Result:
    """Spec §15: say which layout this is, and check the WSL2 rules when that is the one."""
    repo = config.REPO_ROOT if repo is None else repo
    if platform == "win32":
        return Result(PASS, "topology", "native Windows: browser, backend, `claude` login and Docker Desktop all on "
                                       "the host (spec §15)")
    if platform.startswith("linux") and "microsoft" in proc_version.lower():
        if repo.as_posix().startswith("/mnt/"):
            return Result(WARN, "topology", f"WSL2, but the repo is at {repo}: under /mnt, CUDA model loading and "
                                           "file I/O are much slower. Keep it on the WSL2 filesystem (spec §15).")
        return Result(PASS, "topology", "WSL2: backend, `claude` login and Docker here; the browser on Windows. "
                                       "nvidia-smi must work inside WSL2 (see the CUDA line).")
    return Result(PASS, "topology", "Linux: everything on the host (spec §15 reference)")


def _proc_version() -> str:
    try:
        return Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- main
def collect(cfg, *, live: bool = False, skip_probe: bool = False) -> list[Result]:
    """Every check, in the spec's order, with each check's real effects."""
    results: list[Result] = [check_claude_cli(), check_api_key_absent()]
    if not skip_probe:
        results.append(check_claude_probe(cfg))
    results.append(check_loopback_service("VOICEVOX", str(cfg.VOICEVOX_URL), probe_path="/version"))
    results.append(check_loopback_service("app port", f"http://{cfg.HOST}:{cfg.PORT}", down=PASS))
    results.append(check_loopback_service("SearXNG", str(cfg.SEARXNG_URL)))
    results.append(check_docker())
    results.extend(check_tokens(cfg))
    results.append(check_wanikani_scopes())
    if live:
        results.append(check_srs_live("wanikani", str(cfg.WANIKANI_TOKEN)))
        results.append(check_srs_live("bunpro", str(cfg.BUNPRO_API_TOKEN)))
    results.append(check_gitignore())
    results.append(check_hook())
    results.append(check_cuda(float(cfg.VRAM_WARN_GB)))
    results.append(check_avatar(cfg))
    results.append(detect_topology(proc_version=_proc_version()))
    return results


def report(results: list[Result], table: str | None, out=print) -> int:
    for r in results:
        out(r.line())
    out("")
    if table:
        out("service status (spec §5b, last persisted snapshot):")
        out(table)
    else:
        out("service status (spec §5b): no snapshot yet; it is written the first time the app starts.")
    failed = sum(r.level == FAIL for r in results)
    warned = sum(r.level == WARN for r in results)
    out("")
    out(f"doctor: {len(results)} checks, {failed} failed, {warned} warnings" + ("" if failed else " - ready"))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="make doctor", description="environment pre-flight (spec M0)")
    ap.add_argument("--live", action="store_true",
                    help="also make ONE GET per configured SRS token to prove it authenticates (never by default)")
    ap.add_argument("--skip-claude", action="store_true", help="skip the `claude -p` probe (saves ~10 s)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    try:  # a piped cp1252 stdout must not turn `→` into a traceback
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    cfg = config.load()
    results = collect(cfg, live=args.live, skip_probe=args.skip_claude)
    table = status_table(cfg.path("CACHE_DIR") / "srs" / "status.json")
    return report(results, table, out=lambda s: print(s, flush=True))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except config.ConfigError as exc:      # a malformed settings.json: one line, not a traceback
        sys.exit(str(exc))
