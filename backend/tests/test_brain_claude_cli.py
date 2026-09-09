"""Claude CLI provider: spawn hygiene, event translation, auth assertion, timeout, restart.

Lifecycle tests drive `backend/tests/fake_claude.py`, which emits the real stream-json shapes
pinned in constants.py from a live run — so these cover the actual parser without a subscription.
"""
from __future__ import annotations

import asyncio
import json
import sys
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
        sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else self._session_id
        return [sys.executable, str(Path(__file__).parent / "fake_claude.py"), self.scenario, "--session-id", sid]


async def collect(brain, text="こんにちは"):
    return [ev async for ev in brain.turn(text)]


# ------------------------------------------------------------------ interface
def test_claude_provider_satisfies_the_brain_protocol(tmp_path):
    assert isinstance(ClaudeCliBrain(cfg(tmp_path)), Brain)


def test_factory_builds_the_configured_provider_and_rejects_unknown(tmp_path):
    assert create_brain(cfg(tmp_path)).name == "claude-cli"
    with pytest.raises(ValueError, match="unknown BRAIN_PROVIDER"):
        create_brain(cfg(tmp_path, BRAIN_PROVIDER="gpt-9"))


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
    monkeypatch.setattr(config, "claude_cwd", lambda c: config.REPO_ROOT / ".cache" / "claude-cwd")
    b = ClaudeCliBrain(cfg(tmp_path))
    with pytest.raises(RuntimeError, match="inside the repo"):
        asyncio.run(b._spawn(resume=False))


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
