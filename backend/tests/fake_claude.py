"""A stand-in for the `claude` CLI, used by the brain's lifecycle tests.

Emits real-shaped stream-json (the shapes pinned in backend/constants.py from live runs) so the
provider's spawn, init assertions, timeout, interrupt and restart paths can be exercised without a
network call, a subscription, or a 30-second test.

    python -m backend.tests.fake_claude <scenario> [--session-id X] [--resume X] ...

Scenarios (the special behaviour is the FIRST turn of a FRESH process; later turns, and every
turn of a `--resume`d process, answer 「はい。」 — a resumed session answers normally):

    ok · two_sentences · tool_call · bad_auth · no_init · wrong_session · die_mid_turn
    slow          a delta every 50 ms for 10 s; honours a control_request/interrupt the way the
                  real CLI does (verified 2026-09-12): control_response, then an
                  error_during_execution result, then the next turn is answered
    hang          nothing for 60 s; honours an interrupt likewise
    late_result   IGNORES interrupts, says nothing for 2 s, then answers — a wedged turn that
                  eventually produces a stale result
    error_result  a result with is_error (an API error), no text
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time

SESSION = "11111111-2222-3333-4444-555555555555"

# The child inherits the production env allowlist, which deliberately has no PYTHONIOENCODING.
# The real CLI is Node and speaks UTF-8 regardless; this stand-in has to say so itself, or it
# dies on the first Japanese character under the Windows default codepage.
sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")

_lines: queue.Queue = queue.Queue()


def _pump_stdin() -> None:
    for raw in sys.stdin:
        _lines.put(raw)
    _lines.put(None)


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


def result(res: str | None, ttft: int = 100, dur: int = 200, is_error: bool = False, subtype: str | None = None) -> None:
    emit({"type": "result", "subtype": subtype or ("error" if is_error else "success"), "is_error": is_error,
          "result": res, "ttft_ms": ttft, "duration_ms": dur, "num_turns": 1,
          "session_id": SESSION, "uuid": "u-res", "usage": {"input_tokens": 10, "output_tokens": 5}})


def interrupted(request_id: str) -> None:
    """What the real CLI sends back to a control_request/interrupt (verified 2026-09-12)."""
    emit({"type": "control_response", "response": {"subtype": "success", "request_id": request_id}})
    result(None, is_error=True, subtype="error_during_execution")


def wait(seconds: float, honour_interrupt: bool) -> str:
    """Idle for `seconds`, watching stdin. Returns "interrupted", "eof" or "elapsed"."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "elapsed"
        try:
            raw = _lines.get(timeout=min(remaining, 0.05))
        except queue.Empty:
            continue
        if raw is None:
            return "eof"
        msg = json.loads(raw) if raw.strip() else {}
        if msg.get("type") == "control_request" and honour_interrupt:
            interrupted(str(msg.get("request_id", "")))
            return "interrupted"
        # a user turn during a turn, or an ignored interrupt: dropped, like a wedged CLI would


def main() -> int:
    args = sys.argv[1:]
    scenario = args[0] if args else "ok"
    sid = args[args.index("--session-id") + 1] if "--session-id" in args else SESSION
    resumed = "--resume" in args
    if resumed:
        sid = args[args.index("--resume") + 1]
    threading.Thread(target=_pump_stdin, daemon=True).start()

    turn = 0
    while True:
        raw = _lines.get()
        if raw is None:
            return 0
        if not raw.strip():
            continue
        msg = json.loads(raw)
        if msg.get("type") != "user":
            if msg.get("type") == "control_request":     # nothing in flight: acknowledge only
                emit({"type": "control_response", "response": {"subtype": "success",
                                                               "request_id": msg.get("request_id")}})
            continue
        turn += 1
        mode = scenario if (turn == 1 and not resumed) else "ok"
        if turn == 1:
            # The real CLI emits `init` only after reading the first turn (verified 2026-09-09).
            if mode != "no_init":
                init(api_key_source="ANTHROPIC_API_KEY" if mode == "bad_auth" else "none",
                     session_id="99999999-0000-0000-0000-000000000000" if mode == "wrong_session" else sid,
                     tools=["mcp__bunpro__get_ghost_reviews"])
            if mode in ("bad_auth", "no_init"):
                wait(30, honour_interrupt=False)
                return 0
        if mode == "wrong_session":
            wait(30, honour_interrupt=False)
            return 0
        if mode == "hang":
            if wait(60, honour_interrupt=True) == "eof":
                return 0
            continue
        if mode == "slow":
            for _ in range(200):
                text("あ")
                outcome = wait(0.05, honour_interrupt=True)
                if outcome == "interrupted":
                    break
                if outcome == "eof":
                    return 0
            else:
                result("あ" * 200)
            continue
        if mode == "late_result":
            if wait(2.0, honour_interrupt=False) == "eof":
                return 0
            text("遅い答え")
            result("遅い答え")
            continue
        if mode == "error_result":
            result("API Error: 529 overloaded", is_error=True)
            continue
        if mode == "die_mid_turn":
            text("途中まで")
            return 3
        if mode == "tool_call":
            emit({"type": "assistant", "session_id": SESSION, "uuid": "u-a", "message": {
                "role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "mcp__bunpro__get_ghost_reviews", "input": {}}]}})
            emit({"type": "user", "session_id": SESSION, "uuid": "u-u", "message": {
                "role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": '{"count": 1}'}]}})
            text("ゴーストは一つです。")
            result("ゴーストは一つです。")
            continue
        if mode == "two_sentences":
            emit({"type": "rate_limit_event", "session_id": SESSION, "uuid": "u-r",
                  "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}})
            for part in ["[happy]よく", "できました", "。", "でも、", "ここは違います。"]:
                text(part)
            result("[happy]よくできました。でも、ここは違います。")
            continue
        text("はい。")
        result("はい。")


if __name__ == "__main__":
    sys.exit(main())
