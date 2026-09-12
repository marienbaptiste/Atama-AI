"""Claude CLI provider: spawn hygiene, event translation, auth assertion, timeout, restart.

Lifecycle tests drive `backend/tests/fake_claude.py`, which emits the real stream-json shapes
pinned in constants.py from a live run — so these cover the actual parser without a subscription.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from pathlib import Path

import pytest

from backend import config, constants
from backend.brain import (
    Brain,
    BrainError,
    RateLimited,
    TextDelta,
    Thinking,
    ToolCall,
    ToolOutcome,
    TurnComplete,
)
from backend.brain import create as create_brain
from backend.brain.claude_cli import ClaudeCliBrain, child_env

FIXTURE = Path(__file__).parent / "fixtures" / "claude" / "turn_with_tool_call.jsonl"


def cfg(tmp_path, **env):
    return config.load(tmp_path / "settings.json", env={"CACHE_DIR": str(tmp_path), **env})


class FakeCli(ClaudeCliBrain):
    """Runs backend/tests/fake_claude.py instead of the real CLI."""

    scenario = "ok"

    def _launch_argv(self, resume: bool, env: dict[str, str]) -> list[str]:
        argv = self._argv(resume)  # still built, so flag assertions stay meaningful
        self.last_argv = argv
        flag = "--resume" if resume else "--session-id"
        return [sys.executable, str(Path(__file__).parent / "fake_claude.py"), self.scenario, flag, self._session_id]

    async def _spawn(self, resume: bool) -> None:
        try:
            await super()._spawn(resume)
        finally:                                     # every process ever launched, even by a cancelled start
            if self._proc is not None and self._proc not in getattr(self, "procs", []):
                self.procs = [*getattr(self, "procs", []), self._proc]


async def collect(brain, text="こんにちは"):
    return [ev async for ev in brain.turn(text)]


# ------------------------------------------------------------------ interface
def test_claude_provider_satisfies_the_brain_protocol(tmp_path):
    assert isinstance(ClaudeCliBrain(cfg(tmp_path)), Brain)


def test_factory_builds_the_configured_provider_and_rejects_unknown(tmp_path):
    assert create_brain(cfg(tmp_path)).name == "claude-cli"
    # The factory's own guard, independent of whatever validation config.load applies first.
    with pytest.raises(ValueError, match="unknown BRAIN_PROVIDER"):
        create_brain(types.SimpleNamespace(BRAIN_PROVIDER="gpt-9"))


# ----------------------------------------------------------------- spawn args
def test_argv_carries_every_spec_required_flag(tmp_path):
    b = ClaudeCliBrain(cfg(tmp_path), mcp_config=tmp_path / "mcp.json", allowed_tools=["mcp__bunpro__x"],
                       system_prompt="SENSEI")
    argv = b._argv(resume=False)
    pairs = list(zip(argv, argv[1:]))
    assert argv[:2] == ["claude", "-p"]
    assert ("--input-format", "stream-json") in pairs and ("--output-format", "stream-json") in pairs
    assert "--include-partial-messages" in argv and "--verbose" in argv
    assert ("--tools", "") in pairs                      # no built-in tools at all (ADR-016)
    assert "--strict-mcp-config" in argv                 # never inherit the user's MCP servers
    assert ("--session-id", b.session_id) in pairs       # orchestrator-assigned
    assert ("--effort", cfg(tmp_path).CLAUDE_EFFORT) in pairs   # spec §10 lever, configurable
    assert ("--allowedTools", "mcp__bunpro__x") in pairs
    # The prompt goes as a FILE: the string variants truncate multi-line values at the first
    # newline and swallow following flags (verified 2026-09-09).
    prompt_pair = next(p for p in pairs if p[0].endswith("system-prompt-file"))
    assert prompt_pair[0] == "--system-prompt-file"          # replace, not append (persona)
    assert Path(prompt_pair[1]).read_text(encoding="utf-8") == "SENSEI"
    assert not any(a.startswith("--append-system-prompt") or a == "--system-prompt" for a in argv)
    assert argv.index("--mcp-config") < argv.index(prompt_pair[0])


def test_append_mode_uses_the_file_variant_too(tmp_path):
    b = ClaudeCliBrain(cfg(tmp_path), system_prompt="X", replace_system_prompt=False)
    assert "--append-system-prompt-file" in b._argv(resume=False)


@pytest.mark.parametrize("forbidden", ["--bare", "--dangerously-skip-permissions", "--no-session-persistence"])
def test_argv_never_contains_a_forbidden_flag(tmp_path, forbidden):
    assert forbidden not in ClaudeCliBrain(cfg(tmp_path))._argv(resume=False)


def test_resume_replaces_session_id(tmp_path):
    b = ClaudeCliBrain(cfg(tmp_path))
    argv = b._argv(resume=True)
    assert ("--resume", b.session_id) in list(zip(argv, argv[1:])) and "--session-id" not in argv


def test_child_env_is_an_allowlist(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-pass")
    monkeypatch.setenv("WANIKANI_TOKEN", "wk-should-never-pass")
    monkeypatch.setenv("BUNPRO_API_TOKEN", "bp-should-never-pass")
    env = child_env()
    assert "ANTHROPIC_API_KEY" not in env and "WANIKANI_TOKEN" not in env and "BUNPRO_API_TOKEN" not in env
    assert "PATH" in env
    assert set(env) <= set(constants.CLAUDE_CHILD_ENV_ALLOWLIST)


def test_spawn_refuses_a_cwd_inside_the_repo(tmp_path, monkeypatch):
    inside = config.REPO_ROOT / ".cache" / f"claude-cwd-refused-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(config, "claude_cwd", lambda c: inside)
    b = ClaudeCliBrain(cfg(tmp_path))
    with pytest.raises(RuntimeError, match="inside the repo"):
        asyncio.run(b._spawn(resume=False))
    assert not inside.exists()                           # refused BEFORE anything was created


# ------------------------------------------------------------ event translation
def test_translate_covers_the_real_captured_stream(tmp_path):
    """The golden fixture is a real turn (sanitised) in which the tutor called an MCP tool."""
    b = ClaudeCliBrain(cfg(tmp_path))
    events = [e for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip() for e in b._translate(line)]
    kinds = {type(e) for e in events}
    assert TextDelta in kinds and Thinking in kinds and ToolCall in kinds and ToolOutcome in kinds
    spoken = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "そういう" in spoken
    call = next(e for e in events if isinstance(e, ToolCall))
    assert call.name == "mcp__bunpro__get_ghost_reviews"
    assert next(e for e in events if isinstance(e, ToolOutcome)).ok is True
    done = [e for e in events if isinstance(e, TurnComplete)]
    assert len(done) == 1 and done[0].ttft_ms and done[0].duration_ms


def test_translate_skips_unknown_and_malformed_lines(tmp_path):
    b = ClaudeCliBrain(cfg(tmp_path))
    assert b._translate("not json") == []
    assert b._translate(json.dumps({"type": "some_future_event", "payload": 1})) == []
    assert b._translate(json.dumps({"type": "system", "subtype": "status", "status": "requesting"})) == []


def test_translate_rate_limit_only_when_not_allowed(tmp_path):
    b = ClaudeCliBrain(cfg(tmp_path))
    allowed = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}}
    hit = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}}
    assert b._translate(json.dumps(allowed)) == []
    assert isinstance(b._translate(json.dumps(hit))[0], RateLimited)


def test_translate_error_result_yields_error_then_completion(tmp_path):
    b = ClaudeCliBrain(cfg(tmp_path))
    out = b._translate(json.dumps({"type": "result", "is_error": True, "result": "boom", "duration_ms": 5}))
    assert isinstance(out[0], BrainError) and isinstance(out[1], TurnComplete)


# ----------------------------------------------------------------- lifecycle
def test_turn_streams_text_and_completes(tmp_path):
    async def go():
        b = FakeCli(cfg(tmp_path))
        b.scenario = "two_sentences"
        await b.start()
        events = await collect(b)
        await b.aclose()
        return events

    events = asyncio.run(go())
    spoken = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert spoken == "[happy]よくできました。でも、ここは違います。"
    done = events[-1]
    assert isinstance(done, TurnComplete) and done.text == spoken and done.ttft_ms == 100


def test_tool_call_and_outcome_are_surfaced_with_tool_time(tmp_path):
    async def go():
        b = FakeCli(cfg(tmp_path))
        b.scenario = "tool_call"
        await b.start()
        events = await collect(b)
        await b.aclose()
        return events

    events = asyncio.run(go())
    assert any(isinstance(e, ToolCall) for e in events)
    assert any(isinstance(e, ToolOutcome) and e.ok for e in events)
    assert isinstance(events[-1], TurnComplete) and events[-1].tool_ms is not None


def test_refuses_to_continue_when_an_api_key_reached_the_child(tmp_path):
    """apiKeySource != "none" means the session would be billed to the API (ADR-001).

    The check lands on the first turn, not on start(): the CLI emits `init` only once it has
    read a turn (verified 2026-09-09), so waiting for it beforehand deadlocks.
    """
    async def go():
        b = FakeCli(cfg(tmp_path, CLAUDE_TURN_TIMEOUT_S="10"))
        b.scenario = "bad_auth"
        await b.start()
        events = await collect(b)
        await b.aclose()
        return events

    events = asyncio.run(go())
    fatal = [e for e in events if isinstance(e, BrainError) and e.fatal]
    assert fatal and "apiKeySource" in fatal[0].message
    assert isinstance(events[-1], TurnComplete)


def test_init_is_captured_from_the_first_turn(tmp_path):
    async def go():
        b = FakeCli(cfg(tmp_path))
        await b.start()
        assert b.last_init == {}          # nothing yet: the CLI has not been given a turn
        await collect(b)
        await b.aclose()
        return b.last_init

    init = asyncio.run(go())
    assert init.get("apiKeySource") == "none" and init.get("tools")


def test_wedged_turn_is_interrupted_and_the_app_survives(tmp_path):
    async def go():
        b = FakeCli(cfg(tmp_path, CLAUDE_TURN_TIMEOUT_S="1"))
        b.scenario = "hang"
        await b.start()
        events = await collect(b)
        await b.aclose()
        return events

    events = asyncio.run(go())
    assert any(isinstance(e, BrainError) and "exceeded" in e.message for e in events)
    assert isinstance(events[-1], TurnComplete)  # the turn always closes


def test_crash_mid_turn_restarts_with_resume_and_keeps_the_session_id(tmp_path):
    async def go():
        b = FakeCli(cfg(tmp_path))
        b.scenario = "die_mid_turn"
        await b.start()
        sid = b.session_id
        events = await collect(b)
        argv = b.last_argv
        await b.aclose()
        return events, sid, b.session_id, argv

    events, sid_before, sid_after, argv = asyncio.run(go())
    assert any(isinstance(e, BrainError) and e.fatal for e in events)
    assert isinstance(events[-1], TurnComplete)
    assert sid_after == sid_before                       # memory survives (spec §4)
    assert ("--resume", sid_before) in list(zip(argv, argv[1:]))


def test_status_is_reported_as_brain_not_claude(tmp_path):
    from backend.status import StatusRegistry

    async def go():
        reg = StatusRegistry()
        b = FakeCli(cfg(tmp_path), registry=reg)
        await b.start()
        await collect(b)
        await b.aclose()
        return reg

    reg = asyncio.run(go())
    st = reg.get("brain")
    assert st is not None and st.state == "ready" and "claude-cli" in st.detail
    assert reg.get("claude") is None  # provider-agnostic chip (ADR-027)


# ------------------------------------------------ compaction, as the CLI announces it (2026-09-11)
# Verbatim shapes from a live `/compact` over stream-json (constants.py), trimmed of uuids.
COMPACTING = {"type": "system", "subtype": "status", "status": "compacting", "session_id": "s"}
COMPACT_OK = {"type": "system", "subtype": "status", "status": None, "compact_result": "success",
              "session_id": "s"}
COMPACT_FAILED = {"type": "system", "subtype": "status", "status": None, "compact_result": "failed",
                  "compact_error": "Not enough messages to compact.", "session_id": "s"}
BOUNDARY = {"type": "system", "subtype": "compact_boundary", "session_id": "s",
            "compact_metadata": {"trigger": "auto", "pre_tokens": 24546, "post_tokens": 780,
                                 "duration_ms": 11879, "preserved_segment": {}, "preserved_messages": {}}}


def test_a_compaction_is_announced_start_and_end_with_its_figures(tmp_path):
    from backend.brain import Compacting
    b = ClaudeCliBrain(cfg(tmp_path))
    assert b._translate(json.dumps(COMPACTING)) == [Compacting(active=True)]
    assert b._translate(json.dumps(COMPACT_OK)) == []          # the boundary carries the figures
    assert b._translate(json.dumps(BOUNDARY)) == [
        Compacting(active=False, trigger="auto", pre_tokens=24546, post_tokens=780, duration_ms=11879)]


def test_a_failed_compaction_is_reported_once(tmp_path):
    from backend.brain import Compacting
    b = ClaudeCliBrain(cfg(tmp_path))
    b._translate(json.dumps(COMPACTING))
    first = b._translate(json.dumps(COMPACT_FAILED))
    assert first == [Compacting(active=False, error="Not enough messages to compact.")]
    assert b._translate(json.dumps(COMPACT_FAILED)) == []     # the CLI said it twice; we say it once


def test_other_status_events_are_not_compactions(tmp_path):
    b = ClaudeCliBrain(cfg(tmp_path))
    assert b._translate(json.dumps({"type": "system", "subtype": "status", "status": "requesting"})) == []


# ------------------------------------------------- stopping a turn: barge-in, timeout, errors
# The wire protocol (control_request/interrupt, error_during_execution) was verified live on
# 2026-09-12 and is pinned in constants.py; fake_claude.py replays it.
def _spoken(events) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


def test_a_barge_in_stops_the_turn_and_the_next_turn_hears_only_its_own_text(tmp_path):
    """The bug (2026-09-12): cancelling the consumer left the CLI generating, and the old turn's
    deltas and result replayed into the next answer."""
    async def go():
        b = FakeCli(cfg(tmp_path))
        b.scenario = "slow"
        await b.start()
        heard = []

        async def listen():
            async for ev in b.turn("長い話をして"):
                if isinstance(ev, TextDelta):
                    heard.append(ev.text)

        task = asyncio.create_task(listen())
        while len(heard) < 3:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        events = await collect(b, "次の質問")
        await b.aclose()
        return heard, events, b

    heard, events, b = asyncio.run(go())
    assert heard and _spoken(events) == "はい。"                 # nothing of the old turn
    assert isinstance(events[-1], TurnComplete) and events[-1].text == "はい。"
    assert not any(isinstance(e, BrainError) for e in events)   # a deliberate stop is not an error
    assert len(b.procs) == 1 and "--resume" not in b.last_argv  # the same process, the same session


def test_a_timed_out_turn_does_not_leak_its_late_result_into_the_next(tmp_path):
    async def go():
        b = FakeCli(cfg(tmp_path, CLAUDE_TURN_TIMEOUT_S="1"))
        b.scenario = "late_result"                          # ignores the interrupt, answers at 2 s
        await b.start()
        first = await collect(b)
        second = await collect(b, "次")
        await b.aclose()
        return first, second, b

    first, second, b = asyncio.run(go())
    assert any(isinstance(e, BrainError) and "exceeded" in e.message for e in first)
    assert _spoken(second) == "はい。" and "遅い答え" not in _spoken(second)
    assert not any(isinstance(e, BrainError) for e in second)
    assert len(b.procs) == 1                                    # the late result was drained, not respawned


def test_a_turn_that_never_closes_is_killed_and_the_session_resumed_silently(tmp_path):
    async def go():
        b = FakeCli(cfg(tmp_path, CLAUDE_TURN_TIMEOUT_S="1"))
        b.scenario = "late_result"
        b.interrupt_grace_s = 0.3                           # shorter than the 2 s the fake stalls
        await b.start()
        await collect(b)
        second = await collect(b, "次")
        await b.aclose()
        return second, b

    second, b = asyncio.run(go())
    assert _spoken(second) == "はい。" and not any(isinstance(e, BrainError) for e in second)
    assert len(b.procs) == 2 and ("--resume", b.session_id) in list(zip(b.last_argv, b.last_argv[1:]))
    assert b.procs[0].poll() is not None                        # the wedged process is gone, and reaped


def test_an_error_result_closes_the_turn_and_the_next_one_is_clean(tmp_path):
    async def go():
        b = FakeCli(cfg(tmp_path))
        b.scenario = "error_result"
        await b.start()
        first = await collect(b)
        second = await collect(b, "次")
        await b.aclose()
        return first, second

    first, second = asyncio.run(go())
    assert [type(e) for e in first] == [BrainError, TurnComplete] and "529" in first[0].message
    assert not first[0].fatal
    assert _spoken(second) == "はい。" and not any(isinstance(e, BrainError) for e in second)


# ------------------------------------------------------------- init assertions (spec §4)
def test_a_fatal_auth_failure_is_sticky_and_never_respawns(tmp_path):
    async def go():
        b = FakeCli(cfg(tmp_path, CLAUDE_TURN_TIMEOUT_S="10"))
        b.scenario = "bad_auth"
        await b.start()
        first = await collect(b)
        second = await collect(b)
        return first, second, b

    first, second, b = asyncio.run(go())
    assert any(isinstance(e, BrainError) and e.fatal and "apiKeySource" in e.message for e in first)
    assert [type(e) for e in second] == [BrainError, TurnComplete] and second[0].fatal
    assert "apiKeySource" in second[0].message
    assert len(b.procs) == 1 and b._proc is None                # refused, and it stayed refused


def test_init_must_report_the_session_id_we_assigned(tmp_path):
    async def go():
        b = FakeCli(cfg(tmp_path, CLAUDE_TURN_TIMEOUT_S="10"))
        b.scenario = "wrong_session"
        await b.start()
        events = await collect(b)
        await b.aclose()
        return events, b.session_id

    events, sid = asyncio.run(go())
    fatal = [e for e in events if isinstance(e, BrainError) and e.fatal]
    assert fatal and "session" in fatal[0].message and sid in fatal[0].message
    assert isinstance(events[-1], TurnComplete)


# ------------------------------------------------------------------- process hygiene
def test_a_cancelled_start_closes_the_process_it_launched(tmp_path):
    """A rotation discarded mid-spawn (session.discard) lands a CancelledError inside start()
    after Popen has run; the process must not outlive the brain that never finished starting."""
    async def go():
        b = FakeCli(cfg(tmp_path), mcp_ready_markers={"bunpro_mcp": tmp_path / "never-written"})
        task = asyncio.create_task(b.start())
        await asyncio.sleep(0.3)                            # inside _await_mcp_ready, Popen done
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return b

    b = asyncio.run(go())
    assert b._proc is None and len(b.procs) == 1
    assert b.procs[0].poll() is not None                        # closed and reaped


def test_close_reaps_a_process_that_will_not_exit_on_its_own(tmp_path):
    async def go():
        b = FakeCli(cfg(tmp_path))
        b.scenario = "hang"
        await b.start()
        b.close_grace_s = 0.2
        proc = b._proc
        proc.stdin = None                                   # no EOF for it: only the kill remains
        await b.aclose()
        return proc

    proc = asyncio.run(go())
    assert proc.poll() is not None


def test_each_brain_writes_its_own_prompt_file(tmp_path):
    """One shared .cache/tutor_prompt_rendered.txt was written by the tutor, the summariser, the
    explanation worker and every rotation replacement, and unlinked by whichever closed first."""
    async def go():
        a, b = FakeCli(cfg(tmp_path), system_prompt="A"), FakeCli(cfg(tmp_path), system_prompt="B")
        await a.start()
        await b.start()
        fa, fb = a._prompt_file, b._prompt_file
        await a.aclose()
        alive = fb.exists() and fb.read_text(encoding="utf-8") == "B"
        await b.aclose()
        return fa, fb, alive, fb.exists()

    fa, fb, alive_after_a_closed, left_behind = asyncio.run(go())
    assert fa != fb and tmp_path in fa.parents and tmp_path in fb.parents
    assert alive_after_a_closed and not left_behind


def test_the_last_turn_carries_its_tools_and_usage_for_the_turn_log(tmp_path):
    async def go():
        b = FakeCli(cfg(tmp_path))
        b.scenario = "tool_call"
        await b.start()
        events = await collect(b)
        await b.aclose()
        return events, b.last_turn

    events, last = asyncio.run(go())
    [outcome] = [e for e in events if isinstance(e, ToolOutcome)]
    assert outcome.name == "mcp__bunpro__get_ghost_reviews"     # paired with its call
    assert [t["name"] for t in last["tools"]] == ["mcp__bunpro__get_ghost_reviews"]
    assert last["tools"][0]["ok"] is True and isinstance(last["tools"][0]["ms"], int)
    assert last["usage"].get("input_tokens") == 10
