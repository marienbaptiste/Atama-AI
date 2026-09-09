"""A stand-in for the `claude` CLI, used by the brain's lifecycle tests.

Emits real-shaped stream-json (the shapes pinned in backend/constants.py from a live run) so the
provider's spawn, init assertion, timeout and restart paths can be exercised without a network
call, a subscription, or a 30-second test.

    python -m backend.tests.fake_claude <scenario> [--session-id X] [--resume X] ...

Scenarios: ok · two_sentences · tool_call · bad_auth · die_mid_turn · hang · no_init
"""
from __future__ import annotations

import json
import sys
import time

SESSION = "11111111-2222-3333-4444-555555555555"

# The child inherits the production env allowlist, which deliberately has no PYTHONIOENCODING.
# The real CLI is Node and speaks UTF-8 regardless; this stand-in has to say so itself, or it
# dies on the first Japanese character under the Windows default codepage.
sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def init(api_key_source: str = "none", session_id: str = SESSION, tools=()) -> None:
    emit({"type": "system", "subtype": "init", "session_id": session_id, "uuid": "u-init",
          "apiKeySource": api_key_source, "model": "claude-fake", "cwd": "/tmp",
          "tools": list(tools), "mcp_servers": [{"name": "bunpro", "status": "pending"}]})


def text(s: str) -> None:
    emit({"type": "stream_event", "session_id": SESSION, "uuid": "u",
          "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": s}}})


def result(res: str, ttft: int = 100, dur: int = 200, is_error: bool = False) -> None:
    emit({"type": "result", "subtype": "error" if is_error else "success", "is_error": is_error,
          "result": res, "ttft_ms": ttft, "duration_ms": dur, "num_turns": 1,
          "session_id": SESSION, "uuid": "u-res", "usage": {"input_tokens": 10, "output_tokens": 5}})


def main() -> int:
    args = sys.argv[1:]
    scenario = args[0] if args else "ok"
    sid = args[args.index("--session-id") + 1] if "--session-id" in args else SESSION

    first = True
    for raw in sys.stdin:  # one user turn per line
        if not raw.strip():
            continue
        if first:
            first = False
            # The real CLI emits `init` only after reading the first turn (verified 2026-09-09).
            if scenario != "no_init":
                init(api_key_source="ANTHROPIC_API_KEY" if scenario == "bad_auth" else "none",
                     session_id=sid, tools=["mcp__bunpro__get_ghost_reviews"])
            if scenario in ("bad_auth", "no_init"):
                time.sleep(30)
                return 0
        if scenario == "hang":
            time.sleep(60)
            return 0
        if scenario == "die_mid_turn":
            text("途中まで")
            return 3
        if scenario == "tool_call":
            emit({"type": "assistant", "session_id": SESSION, "uuid": "u-a", "message": {
                "role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "mcp__bunpro__get_ghost_reviews", "input": {}}]}})
            emit({"type": "user", "session_id": SESSION, "uuid": "u-u", "message": {
                "role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": '{"count": 1}'}]}})
            text("ゴーストは一つです。")
            result("ゴーストは一つです。")
            continue
        if scenario == "two_sentences":
            emit({"type": "rate_limit_event", "session_id": SESSION, "uuid": "u-r",
                  "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}})
            for part in ["[happy]よく", "できました", "。", "でも、", "ここは違います。"]:
                text(part)
            result("[happy]よくできました。でも、ここは違います。")
            continue
        text("はい。")
        result("はい。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
