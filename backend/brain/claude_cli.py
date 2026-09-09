"""Claude Code CLI provider for the Brain interface (spec §4, ADR-001/016/027).

One persistent `claude -p` subprocess per session, stream-json in and out. Everything the spec
calls for lives here and nowhere else: the allowlisted environment, the out-of-repo cwd, the
`apiKeySource` assertion, the MCP readiness wait, the per-turn timeout, and `--resume` on crash.

All flag behaviour is pinned in `backend/constants.py` against Claude Code 2.1.159 (ADR-015).
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from backend import config, constants
from backend.brain import (
    BrainError,
    RateLimited,
    TextDelta,
    Thinking,
    ToolCall,
    ToolOutcome,
    TurnComplete,
)

_EOF = object()


class ClaudeCliBrain:
    """The `claude` CLI as a Brain. See ADR-027 for why this is behind an interface."""

    name = "claude-cli"

    def __init__(
        self,
        cfg: config.Config,
        registry=None,
        *,
        mcp_config: Path | None = None,
        mcp_ready_marker: Path | None = None,
        system_prompt: str = "",
        allowed_tools: Iterable[str] = (),
        model: str | None = None,
        replace_system_prompt: bool | None = None,
    ):
        self._cfg = cfg
        self._registry = registry
        self._mcp_config = mcp_config
        self._mcp_ready = mcp_ready_marker
        self._system_prompt = system_prompt
        self._allowed_tools = tuple(allowed_tools)
        self._model = model or cfg.CLAUDE_MODEL
        replace = getattr(cfg, "CLAUDE_REPLACE_SYSTEM_PROMPT", True) if replace_system_prompt is None else replace_system_prompt
        self._prompt_flag = "--system-prompt-file" if replace else "--append-system-prompt-file"
        self._session_id = str(uuid.uuid4())
        self._proc: subprocess.Popen[str] | None = None
        self._queue: asyncio.Queue[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: threading.Thread | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=50)
        self._started = False
        self._prompt_file: Path | None = None
        self.last_init: dict[str, Any] = {}

    # ------------------------------------------------------------------ public
    @property
    def session_id(self) -> str:
        return self._session_id

    async def start(self) -> None:
        if self._started:
            return
        self._report("starting", f"{self.name} · {self._model}")
        await self._spawn(resume=False)
        self._started = True

    async def turn(self, text: str) -> AsyncIterator:
        """Take one user turn and yield events until it completes."""
        if not self._started:
            await self.start()
        assert self._proc is not None and self._queue is not None

        if not self._write_turn(text):
            async for ev in self._restart_and_fail("brain process was not accepting input"):
                yield ev
            return

        self._report("thinking")
        deadline = time.monotonic() + float(self._cfg.CLAUDE_TURN_TIMEOUT_S)
        spoken: list[str] = []
        tool_started: float | None = None
        tool_ms = 0.0

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._interrupt()
                yield BrainError(f"turn exceeded {self._cfg.CLAUDE_TURN_TIMEOUT_S}s", fatal=False)
                yield TurnComplete(text="".join(spoken))
                self._report("ready")
                return
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            if item is _EOF:
                async for ev in self._restart_and_fail("brain process exited mid-turn"):
                    yield ev
                return

            try:
                parsed = json.loads(item)
            except ValueError:
                continue
            if parsed.get("type") == "system" and parsed.get("subtype") == "init":
                if problem := self._check_auth(parsed):
                    yield BrainError(problem, fatal=True)
                    await self.aclose()
                    yield TurnComplete(text="".join(spoken))
                    return
                continue

            for event in self._translate_event(parsed):
                if isinstance(event, TextDelta):
                    spoken.append(event.text)
                elif isinstance(event, ToolCall):
                    tool_started = time.monotonic()
                elif isinstance(event, ToolOutcome) and tool_started is not None:
                    tool_ms += (time.monotonic() - tool_started) * 1000.0
                    tool_started = None
                elif isinstance(event, TurnComplete):
                    self._report("ready")
                    yield TurnComplete(
                        text="".join(spoken) or event.text,
                        ttft_ms=event.ttft_ms,
                        duration_ms=event.duration_ms,
                        tool_ms=tool_ms or None,
                        usage=event.usage,
                    )
                    return
                yield event

    async def aclose(self) -> None:
        self._started = False
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.stdin and proc.stdin.close()
            except OSError:
                pass
            try:
                proc.terminate()
                await asyncio.get_running_loop().run_in_executor(None, proc.wait, 5)
            except Exception:
                proc.kill()
        if self._prompt_file and self._prompt_file.exists():
            self._prompt_file.unlink(missing_ok=True)

    # ------------------------------------------------------------------- spawn
    def _argv(self, resume: bool) -> list[str]:
        argv = list(constants.CLAUDE_BASE_ARGS)
        argv += ["--model", self._model]
        if self._cfg.CLAUDE_FALLBACK_MODEL:
            argv += ["--fallback-model", self._cfg.CLAUDE_FALLBACK_MODEL]
        if self._cfg.CLAUDE_EFFORT:
            # The CLI lever for spec §10's "extended thinking OFF": deliberation is silence.
            argv += ["--effort", self._cfg.CLAUDE_EFFORT]
        if resume:
            argv += ["--resume", self._session_id]
        else:
            argv += ["--session-id", self._session_id]
        if self._mcp_config:
            argv += ["--mcp-config", str(self._mcp_config)]  # --strict-mcp-config is in the base args
        if self._allowed_tools:
            argv += ["--allowedTools", ",".join(self._allowed_tools)]
        if self._system_prompt:
            # Pass the prompt as a FILE, never as an argv string. Verified 2026-09-09: a
            # multi-line value given to --system-prompt / --append-system-prompt is truncated at
            # the first newline (the model received only line 1), and any flag placed after it
            # was swallowed — `--mcp-config` was silently ignored, so the tutor started with no
            # tools. The file variant applies the whole prompt, has no length limit, and keeps
            # the student profile out of `ps`.
            self._prompt_file = self._write_prompt_file(self._system_prompt)
            argv += [self._prompt_flag, str(self._prompt_file)]
        return argv

    def _launch_argv(self, resume: bool, env: dict[str, str]) -> list[str]:
        """Resolve the launcher and return the full command. The seam tests substitute."""
        argv = self._argv(resume)
        exe = shutil.which(argv[0], path=env.get("PATH"))
        if not exe:
            raise RuntimeError(f"{argv[0]!r} not found on PATH. Install the Claude Code CLI (README: Claude login).")
        return [exe, *argv[1:]]

    def _write_prompt_file(self, text: str) -> Path:
        """Keep a copy of the rendered prompt for debugging (spec §11: under .cache/)."""
        path = self._cfg.path("CACHE_DIR") / "tutor_prompt_rendered.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    async def _spawn(self, resume: bool) -> None:
        cwd = config.claude_cwd(self._cfg)
        cwd.mkdir(parents=True, exist_ok=True)
        if cwd == config.REPO_ROOT or config.REPO_ROOT in cwd.parents:
            raise RuntimeError(f"claude cwd {cwd} is inside the repo; it would inherit CLAUDE.md (spec §4)")

        env = child_env()
        if self._mcp_ready:
            self._mcp_ready.unlink(missing_ok=True)  # a stale marker must not read as connected

        # No shell: `claude.CMD` is executed directly. shell=True broke on a cwd outside the repo
        # (2026-09-09) and is an injection surface we do not need.
        self._proc = subprocess.Popen(
            self._launch_argv(resume, env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(cwd),
            env=env,
        )
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._reader = threading.Thread(target=self._pump, args=(self._proc, self._loop, self._queue), daemon=True)
        self._reader.start()
        # stderr MUST be drained continuously. `--verbose` makes the CLI chatty, and once the OS
        # pipe buffer fills the child blocks on write and stops producing stdout entirely — a
        # deadlock that looks exactly like "the model is slow" (found 2026-09-09).
        self._stderr_tail.clear()
        threading.Thread(target=self._pump_stderr, args=(self._proc,), daemon=True).start()

        if self._mcp_ready is not None:
            await self._await_mcp_ready()
        if self._proc.poll() is not None:
            raise RuntimeError(f"claude exited during startup (rc={self._proc.returncode}): "
                               f"{self._drain_stderr() or '(silent)'}")

    def _pump(self, proc: subprocess.Popen[str], loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        """Reader thread: one stdout line -> one queue item. Threads, not asyncio pipes, because
        this is the one path that must behave identically on Windows and POSIX."""
        def hand_over(item) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass  # the loop closed first (shutdown); nothing is waiting for this line

        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if line:
                    hand_over(line)
        except (ValueError, OSError):
            pass
        finally:
            hand_over(_EOF)

    def _pump_stderr(self, proc: subprocess.Popen[str]) -> None:
        """Drain stderr into a bounded tail, so it can never block the child and is available
        for diagnostics when startup fails."""
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                line = line.rstrip()
                if line:
                    self._stderr_tail.append(line)
        except (ValueError, OSError):
            pass

    def _check_auth(self, ev: dict) -> str | None:
        """Assert subscription auth from the `init` event (spec §4). None = fine.

        VERIFIED 2026-09-09: `claude -p --input-format stream-json` emits `init` only AFTER it
        reads the first user turn — waiting for it before sending one deadlocks. So the assertion
        happens on the first turn, and `make doctor` is the pre-flight that catches a bad key
        before the app ever runs.
        """
        self.last_init = ev
        source = ev.get("apiKeySource")
        if source == constants.CLAUDE_INIT_APIKEYSOURCE_SUBSCRIPTION:
            return None
        return (f"refusing to continue: apiKeySource={source!r}, expected "
                f"{constants.CLAUDE_INIT_APIKEYSOURCE_SUBSCRIPTION!r}. An API key reached the child and this "
                "session would be billed to the API instead of your subscription (ADR-001). Run `make doctor`.")

    async def _await_mcp_ready(self) -> None:
        """Wait for the MCP server's own ready signal — never a sleep (spec §4).

        Claude prints `init` with `mcp_servers: pending` and never announces the connection; a
        turn sent before then reaches a model with no tools.
        """
        assert self._mcp_ready is not None
        deadline = time.monotonic() + constants.CLAUDE_MCP_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._mcp_ready.exists():
                self._report_service("bunpro_mcp", "connected", f"ready in {constants.CLAUDE_MCP_READY_TIMEOUT_S:.0f}s window")
                return
            if self._proc is not None and self._proc.poll() is not None:
                break
            await asyncio.sleep(0.05)
        self._report_service("bunpro_mcp", "failed", "no ready signal", "MCP server did not connect in time")

    # ------------------------------------------------------------------- turns
    def _write_turn(self, text: str) -> bool:
        msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        try:
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
            return True
        except (OSError, ValueError, AssertionError):
            return False

    def _translate(self, raw: str) -> list:
        """One stdout line -> zero or more provider-neutral events. Never raises."""
        try:
            return self._translate_event(json.loads(raw))
        except ValueError:
            return []

    def _translate_event(self, ev: dict) -> list:
        """One parsed event -> zero or more provider-neutral events. Never raises."""
        etype = ev.get("type")

        if etype == "stream_event":
            inner = ev.get("event") or {}
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta" and delta.get("text"):
                    return [TextDelta(delta["text"])]
                if dtype == "thinking_delta" and delta.get("thinking"):
                    return [Thinking(delta["thinking"])]
            return []

        if etype == "assistant":
            out = []
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    out.append(ToolCall(str(block.get("name", "")), dict(block.get("input") or {})))
            return out

        if etype == "user":
            out = []
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result":
                    content = block.get("content")
                    summary = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                    out.append(ToolOutcome("", not bool(block.get("is_error")), summary[:300]))
            return out

        if etype == constants.CLAUDE_EVENT_RATE_LIMIT:
            info = ev.get("rate_limit_info") or {}
            status = str(info.get("status", ""))
            if status and status != "allowed":
                return [RateLimited(f"{info.get('rateLimitType', '')} {status}".strip())]
            return []

        if etype == "result":
            if ev.get("is_error"):
                return [
                    BrainError(str(ev.get("result") or ev.get("api_error_status") or "turn failed")),
                    TurnComplete(duration_ms=ev.get("duration_ms")),
                ]
            return [
                TurnComplete(
                    text=str(ev.get("result") or ""),
                    ttft_ms=ev.get("ttft_ms"),
                    duration_ms=ev.get("duration_ms"),
                    usage=dict(ev.get("usage") or {}),
                )
            ]

        return []  # unknown event types are skipped, never fatal (spec §4)

    # ----------------------------------------------------------------- recovery
    async def _restart_and_fail(self, reason: str) -> AsyncIterator:
        """The process died mid-turn: report, restart with --resume, close the turn."""
        self._report("restarting", reason)
        yield BrainError(reason, fatal=True)
        try:
            self._proc = None
            await self._spawn(resume=True)
            self._report("ready", "resumed after restart")
        except Exception as exc:  # noqa: BLE001 - a failed restart must not kill the app
            self._started = False
            self._report("error", "restart failed", f"{type(exc).__name__}: {exc}")
            yield BrainError(f"restart failed: {exc}", fatal=True)
        yield TurnComplete()

    def _interrupt(self) -> None:
        """Stop a wedged turn without losing the session (spec §4)."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                proc.terminate()  # no deliverable SIGINT without a console process group
            else:
                proc.send_signal(signal.SIGINT)
        except (OSError, ValueError):
            pass

    def _drain_stderr(self) -> str:
        """The tail the stderr pump collected. Never reads the pipe here: that would block."""
        return " | ".join(self._stderr_tail)[-400:]

    # ------------------------------------------------------------------ status
    def _report(self, state: str, detail: str = "", last_error: str = "") -> None:
        self._report_service("brain", state, detail or f"{self.name} · {self._model}", last_error)

    def _report_service(self, service: str, state: str, detail: str = "", last_error: str = "") -> None:
        if self._registry is not None:
            try:
                self._registry.report(service, state, detail, last_error)
            except ValueError:
                pass


def child_env() -> dict[str, str]:
    """The allowlisted environment for the subprocess (spec §4, ADR-016).

    An allowlist, not a blocklist: this is what keeps SRS tokens out of the brain and every MCP
    server it spawns, and makes `ANTHROPIC_API_KEY` impossible by construction rather than by
    remembering to strip it.
    """
    return {k: v for k, v in os.environ.items() if k in constants.CLAUDE_CHILD_ENV_ALLOWLIST}
