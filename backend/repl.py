"""M1 text REPL — the brain loop with no audio (spec §12 M1).

Type Japanese, watch sentence chunks come back with their emotion and their timings. This is
the milestone's demoable artefact and the place where the first-chunk latency of §10 becomes
visible: the first sentence should print well before the turn finishes.

    python -m backend.repl            # launch: sync SRS, render prompt, start the brain
    python -m backend.repl --refresh  # force a fresh SRS sync (spec §5 manual Refresh)
    python -m backend.repl --no-srs   # skip SRS entirely (offline / no tokens)

Commands inside the loop: /status  /prompt  /profile  /quit
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
import time

from backend import brain as brain_api
from backend import config, prompt
from backend.chunker import SentenceChunker
from backend.brain import BrainError, RateLimited, TextDelta, Thinking, ToolCall, ToolOutcome, TurnComplete
from backend.srs import profile as profile_api
from backend.status import registry
from backend.tools import mcp_config

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
MCP_TOOLS = tuple(f"mcp__bunpro__{t}" for t in ("get_review_queue", "get_ghost_reviews", "get_grammar_progress"))


def _emotion_tag(emotion: str) -> str:
    return f"[{emotion}]".ljust(11) if emotion else " " * 11


async def run(args: argparse.Namespace) -> int:
    cfg = config.load()
    for secret in cfg.secrets().values():
        registry.register_secret(secret)

    # --- launch: SRS snapshot -> student profile (spec §5, ADR-024) -------------
    print(f"{BOLD}atama-AI · M1 text REPL{RESET}")
    if args.no_srs:
        student = profile_api.StudentProfile()
        print(f"{DIM}SRS skipped (--no-srs){RESET}")
    else:
        t0 = time.monotonic()
        student = profile_api.build(
            cfg.WANIKANI_TOKEN, cfg.BUNPRO_API_TOKEN,
            cfg.path("CACHE_DIR") / "srs", cfg.SRS_CACHE_TTL_S, cfg.SRS_FETCH_BUDGET_S,
            registry, force=args.refresh,
        )
        print(f"{DIM}SRS sync {time.monotonic() - t0:.1f}s{RESET}")

    profile_text = profile_api.render(student)
    rendered = prompt.build(profile_text)
    profile_api.debug_snapshot(student, profile_text, cfg.path("LOG_DIR"), dt.date.today().isoformat())
    sections = " · ".join(f"{k} {v}" for k, v in rendered.sections.items())
    print(f"{DIM}prompt {rendered.tokens} tokens ({sections})"
          f"{' · TRUNCATED: ' + ', '.join(rendered.truncated) if rendered.truncated else ''}{RESET}")

    # --- brain (ADR-027: the REPL talks to the interface, not to Claude) --------
    mcp_json = mcp_config.write(cfg) if cfg.BUNPRO_API_TOKEN else None
    brain = brain_api.create(
        cfg, registry=registry,
        mcp_config=mcp_json,
        mcp_ready_marker=mcp_config.ready_marker(cfg) if mcp_json else None,
        system_prompt=rendered.text,
        allowed_tools=MCP_TOOLS if mcp_json else (),
    )
    try:
        await brain.start()
    except RuntimeError as exc:
        print(f"\n{BOLD}cannot start the brain:{RESET} {exc}\n", file=sys.stderr)
        return 2
    print(registry.table())
    print(f"{DIM}type Japanese and press enter · /status /prompt /profile /quit{RESET}\n")

    try:
        while True:
            try:
                line = (await asyncio.to_thread(input, "> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            if line == "/status":
                print(registry.table())
                continue
            if line == "/prompt":
                print(rendered.text)
                continue
            if line == "/profile":
                print(profile_text)
                continue
            await _one_turn(brain, line)
    finally:
        await brain.aclose()
    return 0


async def _one_turn(brain, text: str) -> None:
    """Send one turn; print each sentence the moment it closes, with its latency."""
    chunker = SentenceChunker()
    started = time.monotonic()
    first_chunk_at: float | None = None
    n = 0
    thinking_chars = 0

    def show(chunks) -> None:
        nonlocal first_chunk_at, n
        for chunk in chunks:
            now = time.monotonic() - started
            first_chunk_at = first_chunk_at if first_chunk_at is not None else now
            n += 1
            print(f"  {DIM}{now:5.2f}s{RESET} {_emotion_tag(chunk.emotion)}{chunk.text}")

    async for event in brain.turn(text):
        if isinstance(event, TextDelta):
            show(chunker.push(event.text))
        elif isinstance(event, Thinking):
            thinking_chars += len(event.text)
        elif isinstance(event, ToolCall):
            print(f"  {DIM}· tool {event.name}{RESET}")
        elif isinstance(event, ToolOutcome):
            print(f"  {DIM}· tool {'ok' if event.ok else 'ERROR'}: {event.summary[:120]}{RESET}")
        elif isinstance(event, RateLimited):
            print(f"  {DIM}· rate limited: {event.detail}{RESET}")
        elif isinstance(event, BrainError):
            print(f"  {BOLD}· error:{RESET} {event.message}")
        elif isinstance(event, TurnComplete):
            show(chunker.close())
            total = time.monotonic() - started
            bits = [f"first chunk {first_chunk_at:.2f}s" if first_chunk_at is not None else "no speech",
                    f"turn {total:.2f}s", f"{n} chunk{'s' if n != 1 else ''}"]
            if event.ttft_ms:
                bits.append(f"ttft {event.ttft_ms:.0f}ms")
            if event.tool_ms:
                bits.append(f"tool {event.tool_ms:.0f}ms")
            if thinking_chars:
                bits.append(f"{BOLD}thinking {thinking_chars} chars{RESET}")
            if chunker.stray_tags:
                bits.append(f"stray tags {chunker.stray_tags}")
            print(f"  {DIM}{' · '.join(bits)}{RESET}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="atama-AI M1 text REPL")
    ap.add_argument("--refresh", action="store_true", help="force a fresh SRS sync")
    ap.add_argument("--no-srs", action="store_true", help="skip WaniKani/Bunpro entirely")
    try:
        return asyncio.run(run(ap.parse_args()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
