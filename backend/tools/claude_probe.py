"""Live probe of the tutor subprocess spawn (ROADMAP V0.2 / V0.8, spec §4).

Spawns `claude -p` exactly as the tutor is spawned: stream-json both ways, --strict-mcp-config,
our mcp.json, an isolated cwd, and an ALLOWLISTED environment. When search is configured it waits
for that server's READY MARKER (written by the server when Claude's `notifications/initialized`
arrives — a real signal, not a timer), then sends one user turn; without it, the spawn carries no
MCP config at all. (Until 2026-09-14 this probed the Bunpro MCP server, retired by ADR-039.) Prints event types, init.tools, init.mcp_servers,
init.memory_paths, apiKeySource, the tool_use/tool_result blocks and the result — never a token.
Saves the raw stream to .cache/claude_probe.jsonl (git-ignored) for fixture pinning.

  python -m backend.tools.claude_probe [tools-empty|disallow|default]
  PROBE_PROMPT="..." overrides the prompt.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from backend import config, constants
from backend.tools import mcp_config

PROMPT_WITH_SEARCH = ("Call the search tool once for 天気, then reply in ONE short line in Japanese naming one "
                      "result title. If the tool errors, reply with the error text.")
PROMPT_NO_TOOLS = "List every tool you can call, by exact name, in ONE line. If there are none, say none."


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in constants.CLAUDE_CHILD_ENV_ALLOWLIST}


def wait_for_marker(path: Path, timeout_s: float, proc: subprocess.Popen) -> float | None:
    """Block until the MCP ready marker exists (or the child dies / timeout). Returns seconds waited."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if path.exists():
            return time.monotonic() - t0
        if proc.poll() is not None:
            return None
        time.sleep(0.05)
    return None


def main() -> int:
    cfg = config.load()
    mode = sys.argv[1] if len(sys.argv) > 1 else "tools-empty"   # tools-empty | disallow | default
    tools = mcp_config.allowed_tools(cfg)
    prompt = os.environ.get("PROBE_PROMPT") or (PROMPT_WITH_SEARCH if tools else PROMPT_NO_TOOLS)
    mcp_json = mcp_config.write(cfg) if tools else None
    ready = mcp_config.markers(cfg)
    for marker in ready.values():
        marker.unlink(missing_ok=True)
    cwd = config.claude_cwd(cfg)
    cwd.mkdir(parents=True, exist_ok=True)
    assert config.REPO_ROOT not in cwd.parents and cwd != config.REPO_ROOT, "claude cwd must be outside the repo"

    base = list(constants.CLAUDE_BASE_ARGS)
    if mode != "tools-empty":
        base = [a for i, a in enumerate(base) if not (a == "--tools" or (i > 0 and base[i - 1] == "--tools"))]
    if mode == "disallow":
        base += ["--disallowedTools", ",".join(constants.CLAUDE_BUILTIN_TOOLS_OBSERVED)]
    session_id = str(uuid.uuid4())
    argv = [*base, "--model", "haiku", "--session-id", session_id, "--max-turns", "4"]
    if mcp_json is not None:
        argv += ["--mcp-config", str(mcp_json), "--allowedTools", ",".join(tools)]
    env = child_env()
    assert not any(k in env for k in ("ANTHROPIC_API_KEY", "BUNPRO_API_TOKEN", "WANIKANI_TOKEN"))
    print(f"mode={mode} cwd={cwd} mcp servers={sorted(ready) or 'none'}")

    # No shell in the spawn path: resolve the launcher (claude.CMD on Windows) and exec it directly.
    # (shell=True + cmd.exe failed with "The current directory is invalid." for the out-of-repo cwd.)
    import shutil
    exe = shutil.which(argv[0], path=env.get("PATH"))
    if not exe:
        print(f"claude-probe: {argv[0]!r} not found on the allowlisted PATH", file=sys.stderr)
        return 2
    child = subprocess.Popen([exe, *argv[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", cwd=str(cwd), env=env)
    for name, marker in ready.items():
        waited = wait_for_marker(marker, timeout_s=20, proc=child)
        print(f"{name} ready marker: {'after %.2fs' % waited if waited is not None else 'NOT seen (timeout or child exit)'}")
    user_msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}
    out, err = child.communicate(json.dumps(user_msg) + "\n", timeout=180)
    raw_path = cfg.path("CACHE_DIR") / "claude_probe.jsonl"
    raw_path.write_text(out, encoding="utf-8")
    print(f"exit={child.returncode} stderr={err.strip()[:300]!r}")

    seen = []
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t, st = ev.get("type"), ev.get("subtype")
        seen.append(f"{t}/{st}" if st else t)
        if t == "system" and st not in (None, "init"):
            print(f"system/{st}:", json.dumps({k: v for k, v in ev.items() if k not in ("type", "subtype", "uuid", "session_id")}, ensure_ascii=False)[:300])
        if (t, st) == ("system", "init"):
            print("apiKeySource:", ev.get("apiKeySource"), "| model:", ev.get("model"), "| session ok:", ev.get("session_id") == session_id)
            print("init.tools:", ev.get("tools"))
            print("init.mcp_servers:", json.dumps(ev.get("mcp_servers")))
            print("init.memory_paths:", ev.get("memory_paths"), "| init.cwd:", ev.get("cwd"))
        elif t == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    print("tool_use:", block.get("name"), json.dumps(block.get("input")))
                elif block.get("type") == "text" and block.get("text"):
                    print("assistant text:", block["text"][:200])
        elif t == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result":
                    content = block.get("content")
                    s = json.dumps(content, ensure_ascii=False) if not isinstance(content, str) else content
                    print("tool_result: is_error=", block.get("is_error"), "|", s[:300])
        elif t == "rate_limit_event":
            print("rate_limit_event:", json.dumps(ev.get("rate_limit_info"))[:200])
        elif t == "result":
            print("result:", {k: ev.get(k) for k in ("subtype", "is_error", "num_turns", "ttft_ms", "duration_api_ms", "stop_reason")})
            print("result text:", str(ev.get("result"))[:300])
    compact = []
    for s in seen:
        if compact and compact[-1][0] == s:
            compact[-1][1] += 1
        else:
            compact.append([s, 1])
    print("event sequence:", " ".join(f"{s}x{n}" if n > 1 else s for s, n in compact))
    print(f"raw stream -> {raw_path.relative_to(config.REPO_ROOT).as_posix()}")
    return 0 if child.returncode == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except config.ConfigError as exc:      # a malformed settings.json: one line, not a traceback
        sys.exit(str(exc))
