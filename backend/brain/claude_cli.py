"""Claude Code CLI provider for the Brain interface (spec §4, ADR-001/016/027).

One persistent `claude -p` subprocess per session, stream-json in and out. Everything the spec
calls for lives here and nowhere else: the allowlisted environment, the out-of-repo cwd, the
`init` assertions (session id, `apiKeySource`), the MCP readiness wait, the per-turn timeout, the
mid-turn interrupt, and `--resume` on crash.

All flag and wire-protocol behaviour is pinned in `backend/constants.py` against Claude Code
2.1.159 (ADR-015).

Turn accounting. A turn the caller stops listening to — a barge-in cancels the consuming task, or
the per-turn timeout fires — is still a turn the CLI owes a `result` for. Every turn written to
stdin counts as pending until its `result` is consumed; the next turn settles that debt first
(`_settle`), dropping everything the old turn still streams, so stale text can never replay into a
new answer. A process that does not answer the interrupt inside the grace is killed as a tree and
the session resumed — silently, because the student asked for the interruption.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from backend import config, constants
from backend.brain import (
    BrainError,
    Compacting,
    RateLimited,
    TextDelta,
    Thinking,
    ToolCall,
    ToolOutcome,
    TurnComplete,
)

_EOF = object()

#: How long an interrupted or timed-out turn is given to close with its `result` before the process
#: is treated as wedged, killed as a tree and resumed. Measured 2026-09-12: the result followed the
#: interrupt within the same millisecond, so this is a ceiling for a stuck CLI, never a wait.
INTERRUPT_GRACE_S = 3.0
#: After stdin is closed the CLI exits on its own (0.55 s measured 2026-09-12, constants.py); past
#: this it is killed, tree and all.
CLOSE_GRACE_S = 5.0


class ClaudeCliBrain:
    """The `claude` CLI as a Brain. See ADR-027 for why this is behind an interface."""

    name = "claude-cli"
    #: Instance-overridable so a test can shorten the wedged-process path.
    interrupt_grace_s = INTERRUPT_GRACE_S
    close_grace_s = CLOSE_GRACE_S

    def __init__(
        self,
        cfg: config.Config,
        registry=None,
        *,
        mcp_config: Path | None = None,
        mcp_ready_markers: dict[str, Path] | None = None,
        system_prompt: str = "",
        allowed_tools: Iterable[str] = (),
        model: str | None = None,
        replace_system_prompt: bool | None = None,
        effort: str | None = None,
    ):
        self._cfg = cfg
        self._registry = registry
        self._mcp_config = mcp_config
        self._mcp_ready = dict(mcp_ready_markers or {})
        self._system_prompt = system_prompt
        self._allowed_tools = tuple(allowed_tools)
        self._model = model or cfg.CLAUDE_MODEL
        #: Thinking budget. The tutor gets the configured one; the side workers (summariser,
        #: explanations) pass "low" because a one-shot JSON extraction has nothing to deliberate
        #: about — NOT for speed: measured 2026-09-12, one summary took 13.7 s at medium and
        #: 52.6 s at low, so that call's latency is variance, not effort (which is also why the
        #: launch no longer waits for it).
        self._effort = cfg.CLAUDE_EFFORT if effort is None else effort
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
        #: Turns written to the CLI whose `result` has not been consumed yet (see module doc).
        self._pending = 0
        #: A failure that ends the session for good (an API key reached the child, spec §4).
        #: Sticky: every later turn repeats it and nothing is respawned.
        self._fatal: str | None = None
        self.last_init: dict[str, Any] = {}
        #: The status bar's numbers, as of the last `result` / `rate_limit_event` (see _meters).
        self.meters: dict[str, Any] = {}
        self.rate_limit: dict[str, Any] = {}
        #: What the last turn cost and called, for the turn log (spec §6b schema: tools[], usage).
        self.last_turn: dict[str, Any] = {"tools": [], "usage": {}}
        #: Inside a compaction the CLI announced (see _compaction).
        self._compacting = False

    # ------------------------------------------------------------------ public
    @property
    def session_id(self) -> str:
        return self._session_id

    async def start(self) -> None:
        if self._started:
            return
        if self._fatal:
            raise RuntimeError(self._fatal)
        self._report("starting", f"{self.name} · {self._model}")
        try:
            await self._spawn(resume=False)
        except BaseException:
            # A start that is cancelled (a rotation discarded mid-spawn) or fails after Popen
            # must not leave the process it launched running.
            await self.aclose()
            raise
        self._started = True

    async def turn(self, text: str) -> AsyncIterator:
        """Take one user turn and yield events until it completes."""
        if self._fatal:
            yield BrainError(self._fatal, fatal=True)
            yield TurnComplete()
            return
        if not self._started:
            await self.start()
        assert self._proc is not None and self._queue is not None

        try:
            await self._settle()
        except Exception as exc:  # noqa: BLE001 - the wedged process could not be replaced
            self._started = False
            self._report("error", "restart failed", f"{type(exc).__name__}: {exc}")
            yield BrainError(f"restart failed: {exc}", fatal=True)
            yield TurnComplete()
            return
        if self._fatal:  # the drained stream carried a bad `init`
            await self.aclose()
            yield BrainError(self._fatal, fatal=True)
            yield TurnComplete()
            return

        if not self._write_turn(text):
            async for ev in self._restart_and_fail("brain process was not accepting input"):
                yield ev
            return

        self._report("thinking")
        deadline = time.monotonic() + float(self._cfg.CLAUDE_TURN_TIMEOUT_S)
        spoken: list[str] = []
        tool_started: float | None = None
        tool_ms = 0.0
        calls: list[str] = []                      # tool names awaiting their outcome, in order
        self.last_turn = {"tools": [], "usage": {}}

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Spec §4: a wedged turn is interrupted, apologised for, and the app goes on.
                    # The CLI answers the interrupt with the turn's `result`; the next turn
                    # drains it (_settle), or replaces the process if it never comes.
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
                    if problem := self._check_init(parsed):
                        self._fatal = problem
                        self._report("error", "refusing to continue", problem)
                        yield BrainError(problem, fatal=True)
                        await self.aclose()
                        yield TurnComplete(text="".join(spoken))
                        return
                    continue
                if parsed.get("type") == constants.CLAUDE_EVENT_RESULT:
                    self._pending -= 1

                for event in self._translate_event(parsed):
                    if isinstance(event, Compacting):
                        # A compaction is the CLI working, not hanging: the timeout restarts when it
                        # starts and when it ends, or a long one would be cut off as a stuck turn.
                        deadline = time.monotonic() + float(self._cfg.CLAUDE_TURN_TIMEOUT_S)
                    elif isinstance(event, TextDelta):
                        spoken.append(event.text)
                    elif isinstance(event, ToolCall):
                        tool_started = time.monotonic()
                        calls.append(event.name)
                    elif isinstance(event, ToolOutcome):
                        ms = (time.monotonic() - tool_started) * 1000.0 if tool_started is not None else 0.0
                        tool_ms += ms
                        tool_started = None
                        name = calls.pop(0) if calls else event.name
                        event = ToolOutcome(name, event.ok, event.summary)
                        self.last_turn["tools"].append({"name": name, "ok": event.ok, "ms": round(ms)})
                    elif isinstance(event, TurnComplete):
                        self.last_turn["usage"] = dict(event.usage)
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
        except asyncio.CancelledError:
            # Barge-in: the caller stopped listening. Tell the CLI to stop generating — it closes
            # the turn with a `result` that the next turn drains — and get out of the way at once:
            # nothing is awaited here, because the student is already talking.
            self._interrupt()
            self._report("ready", "interrupted")
            raise

    async def aclose(self) -> None:
        self._started = False
        proc, self._proc = self._proc, None
        self._pending = 0
        await self._close_process(proc)
        if self._prompt_file and self._prompt_file.exists():
            self._prompt_file.unlink(missing_ok=True)

    # ------------------------------------------------------------------- spawn
    def _argv(self, resume: bool) -> list[str]:
        argv = list(constants.CLAUDE_BASE_ARGS)
        argv += ["--model", self._model]
        if self._cfg.CLAUDE_FALLBACK_MODEL:
            argv += ["--fallback-model", self._cfg.CLAUDE_FALLBACK_MODEL]
        if self._effort:
            # The CLI lever for spec §10's "extended thinking OFF": deliberation is silence.
            argv += ["--effort", self._effort]
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
        """The rendered prompt the CLI reads, under .cache/ (spec §11) and named for THIS
        session: the tutor, the summariser, the explanation worker, a rotation replacement and a
        persona switch all run at once, and one shared file was unlinked by whichever closed first."""
        path = self._cfg.path("CACHE_DIR") / "prompts" / f"{self._session_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    async def _spawn(self, resume: bool) -> None:
        cwd = config.claude_cwd(self._cfg)
        if cwd == config.REPO_ROOT or config.REPO_ROOT in cwd.parents:
            raise RuntimeError(f"claude cwd {cwd} is inside the repo; it would inherit CLAUDE.md (spec §4)")
        cwd.mkdir(parents=True, exist_ok=True)

        env = child_env()
        for marker in self._mcp_ready.values():
            marker.unlink(missing_ok=True)  # a stale marker must not read as connected

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
        self._pending = 0
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._reader = threading.Thread(target=self._pump, args=(self._proc, self._loop, self._queue), daemon=True)
        self._reader.start()
        # stderr MUST be drained continuously. `--verbose` makes the CLI chatty, and once the OS
        # pipe buffer fills the child blocks on write and stops producing stdout entirely — a
        # deadlock that looks exactly like "the model is slow" (found 2026-09-09).
        self._stderr_tail.clear()
        threading.Thread(target=self._pump_stderr, args=(self._proc,), daemon=True).start()

        if self._mcp_ready:
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

    def _check_init(self, ev: dict) -> str | None:
        """Assert the `init` event is ours and on subscription auth (spec §4). None = fine.

        VERIFIED 2026-09-09: `claude -p --input-format stream-json` emits `init` only AFTER it
        reads the first user turn — waiting for it before sending one deadlocks. So the assertion
        happens on the first turn, and `make doctor` is the pre-flight that catches a bad key
        before the app ever runs. VERIFIED 2026-09-12: the id matches on `--resume` as well.
        """
        self.last_init = ev
        reported = ev.get("session_id")
        if reported and reported != self._session_id:
            return (f"refusing to continue: the brain reports session {reported!r} but this conversation "
                    f"is {self._session_id!r} (spec §4). Memory would go to the wrong session; the process "
                    "was not started the way this app starts it.")
        source = ev.get("apiKeySource")
        if source == constants.CLAUDE_INIT_APIKEYSOURCE_SUBSCRIPTION:
            return None
        return (f"refusing to continue: apiKeySource={source!r}, expected "
                f"{constants.CLAUDE_INIT_APIKEYSOURCE_SUBSCRIPTION!r}. An API key reached the child and this "
                "session would be billed to the API instead of your subscription (ADR-001). Run `make doctor`.")

    async def _await_mcp_ready(self) -> None:
        """Wait for every MCP server's own ready signal — never a sleep (spec §4).

        Claude prints `init` with `mcp_servers: pending` and never announces the connection; a
        turn sent before then reaches a model with no tools. Servers that never signal are
        reported and skipped: the conversation continues without their tools.
        """
        pending = dict(self._mcp_ready)
        deadline = time.monotonic() + constants.CLAUDE_MCP_READY_TIMEOUT_S
        while pending and time.monotonic() < deadline:
            for service, marker in list(pending.items()):
                if marker.exists():
                    del pending[service]
                    self._report_service(service, "connected" if service.endswith("_mcp") else "ok",
                                         f"ready in {time.monotonic() - (deadline - constants.CLAUDE_MCP_READY_TIMEOUT_S):.1f}s")
            if not pending:
                return
            if self._proc is not None and self._proc.poll() is not None:
                break
            await asyncio.sleep(0.05)
        for service in pending:
            self._report_service(service, "failed" if service.endswith("_mcp") else "down",
                                 "no ready signal", "MCP server did not connect in time")

    # ------------------------------------------------------------------- turns
    def _write_turn(self, text: str) -> bool:
        msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        if self._write(msg):
            self._pending += 1
            return True
        return False

    def _write(self, msg: dict) -> bool:
        try:
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
            return True
        except (OSError, ValueError, AssertionError):
            return False

    async def _settle(self) -> None:
        """Consume what an interrupted or timed-out turn still owes before a new turn is written.

        Everything the old turn still streams — late deltas, its `control_response`, its `result`
        — is dropped, not spoken. A process that does not close the turn inside the grace is
        wedged: it is killed as a tree and the session resumed, with no error surfaced, because
        the interruption was the student's own. Raises only if the replacement cannot start.
        """
        if self._pending <= 0:
            return
        assert self._queue is not None
        deadline = time.monotonic() + float(self.interrupt_grace_s)
        while self._pending > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if item is _EOF:
                break
            try:
                parsed = json.loads(item)
            except ValueError:
                continue
            if parsed.get("type") == "system" and parsed.get("subtype") == "init":
                if problem := self._check_init(parsed):
                    self._fatal = problem     # turn() closes and reports it
                    return
            elif parsed.get("type") == constants.CLAUDE_EVENT_RESULT:
                self._pending -= 1
        if self._pending > 0:
            self._report("restarting", "a stopped turn did not close; resuming the session")
            proc, self._proc = self._proc, None
            await self._close_process(proc, grace=1.0)
            await self._spawn(resume=True)
            self._report("ready", "resumed after a stuck turn")

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
            self.rate_limit = dict(info)          # kept even when "allowed": the status bar shows it
            status = str(info.get("status", ""))
            if status and status != "allowed":
                return [RateLimited(f"{info.get('rateLimitType', '')} {status}".strip())]
            return []

        if etype == constants.CLAUDE_EVENT_SYSTEM:
            return self._compaction(ev)

        if etype == constants.CLAUDE_EVENT_RESULT:
            if ev.get("is_error"):
                return [
                    BrainError(str(ev.get("result") or ev.get("api_error_status") or "turn failed")),
                    TurnComplete(duration_ms=ev.get("duration_ms")),
                ]
            self.meters = self._meters(dict(ev.get("usage") or {}), ev.get("modelUsage") or {})
            return [
                TurnComplete(
                    text=str(ev.get("result") or ""),
                    ttft_ms=ev.get("ttft_ms"),
                    duration_ms=ev.get("duration_ms"),
                    usage=dict(ev.get("usage") or {}),
                )
            ]

        return []  # unknown event types (control_response among them) are skipped, never fatal (spec §4)

    def _compaction(self, ev: dict) -> list:
        """The CLI's own compaction announcements (shapes verified 2026-09-11, constants.py)."""
        sub = ev.get("subtype")
        if sub == constants.CLAUDE_SUBTYPE_STATUS:
            if ev.get("status") == constants.CLAUDE_STATUS_COMPACTING:
                self._compacting = True
                return [Compacting(active=True)]
            if ev.get("compact_result") == "failed" and self._compacting:
                self._compacting = False       # the CLI reports a failure twice; say it once
                return [Compacting(active=False, error=str(ev.get("compact_error") or "failed"))]
            return []                          # "requesting", or a success the boundary reports
        if sub == constants.CLAUDE_SUBTYPE_COMPACT_BOUNDARY:
            self._compacting = False
            meta = ev.get("compact_metadata") or {}
            return [Compacting(active=False, trigger=str(meta.get("trigger") or ""),
                               pre_tokens=meta.get("pre_tokens"), post_tokens=meta.get("post_tokens"),
                               duration_ms=meta.get("duration_ms"))]
        return []

    @staticmethod
    def _meters(usage: dict, model_usage: dict) -> dict[str, Any]:
        """How full her context is, from one `result` (fields verified 2026-09-10, constants.py).

        Context is what the LAST API call of the turn read — fresh input + cache read + cache
        written — so a turn with a tool call counts its final request, not the sum of both.
        The window comes from the main model's modelUsage entry (the one that read the most; a
        small helper model appears there too). Cost is deliberately not read: the account is a
        subscription through `claude -p`, and dollars mean nothing there (user, 2026-09-10).
        """
        last = (usage.get("iterations") or [usage])[-1] or {}
        context = sum(int(last.get(k) or 0) for k in
                      ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        entries = [m for m in model_usage.values() if isinstance(m, dict)] if isinstance(model_usage, dict) else []
        main = max(entries, default={}, key=lambda m: sum(int(m.get(k) or 0) for k in
                   ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")))
        return {"context_tokens": context or None, "context_window": main.get("contextWindow")}

    # ----------------------------------------------------------------- recovery
    async def _restart_and_fail(self, reason: str) -> AsyncIterator:
        """The process died mid-turn: report, restart with --resume, close the turn."""
        self._report("restarting", reason)
        yield BrainError(reason, fatal=True)
        try:
            proc, self._proc = self._proc, None
            await self._close_process(proc, grace=1.0)      # reap it; it is dead or deaf
            await self._spawn(resume=True)
            self._report("ready", "resumed after restart")
        except Exception as exc:  # noqa: BLE001 - a failed restart must not kill the app
            self._started = False
            self._report("error", "restart failed", f"{type(exc).__name__}: {exc}")
            yield BrainError(f"restart failed: {exc}", fatal=True)
        yield TurnComplete()

    def _interrupt(self) -> None:
        """Stop the turn in flight without losing the session (spec §4).

        A `control_request` of subtype `interrupt` on stdin — the Agent SDK's wire protocol,
        verified 2026-09-12 (constants.py): the CLI acknowledges at once, closes the turn with an
        `error_during_execution` result, and answers the next turn normally. Signals are not
        used: on Windows they reach only the launcher shim, and the session would be lost.
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._write({"type": constants.CLAUDE_CONTROL_REQUEST, "request_id": uuid.uuid4().hex[:12],
                     "request": {"subtype": constants.CLAUDE_CONTROL_INTERRUPT}})

    async def _close_process(self, proc: subprocess.Popen[str] | None, grace: float | None = None) -> None:
        """Close stdin (the CLI exits on EOF, 0.55 s measured), wait, then kill the whole tree —
        and always reap, so no Popen is dropped without a wait()."""
        if proc is None:
            return
        loop = asyncio.get_running_loop()
        with contextlib.suppress(OSError, ValueError):
            proc.stdin and proc.stdin.close()
        if proc.poll() is None:
            try:
                await loop.run_in_executor(None, proc.wait, self.close_grace_s if grace is None else grace)
            except subprocess.TimeoutExpired:
                await loop.run_in_executor(None, kill_tree, proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            await loop.run_in_executor(None, proc.wait, 5)

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


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop the child and everything it spawned (the CLI's MCP servers included).

    Windows: `shutil.which("claude")` is claude.CMD, a cmd.exe shim with claude.exe as its child,
    and TerminateProcess on the shim leaves claude.exe running (measured 2026-09-12); `taskkill /T`
    is the one call that takes the tree. POSIX: the launcher exec()s the binary, so SIGTERM reaches
    it directly.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        with contextlib.suppress(OSError):
            proc.terminate()
    try:
        proc.wait(3)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()


def child_env() -> dict[str, str]:
    """The allowlisted environment for the subprocess (spec §4, ADR-016).

    An allowlist, not a blocklist: this is what keeps SRS tokens out of the brain and every MCP
    server it spawns, and makes `ANTHROPIC_API_KEY` impossible by construction rather than by
    remembering to strip it.
    """
    return {k: v for k, v in os.environ.items() if k in constants.CLAUDE_CHILD_ENV_ALLOWLIST}
