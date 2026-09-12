"""The tutor's command line: launch the lesson, then talk to her — by voice, or by typing.

`python -m backend.repl` builds the lesson (`backend.orchestrator.Lesson`: SRS snapshot, page,
voice, Whisper, memory, prompt, brain) and then either runs the voice loop (`--listen`) or a text
prompt where sentence chunks come back with their emotion and their timings — the place where the
first-chunk latency of §10 is visible: the first sentence prints well before the turn finishes.

    python -m backend.repl            # launch: sync SRS, render prompt, start the brain
    python -m backend.repl --speak    # ...and speak every sentence aloud through VOICEVOX
    python -m backend.repl --listen --speak   # ...and talk to her out loud (full voice loop)
    python -m backend.repl --browser --show   # ...with the avatar page open (make run does this)
    python -m backend.repl --refresh  # force a fresh SRS sync (spec §5 manual Refresh)
    python -m backend.repl --no-srs   # skip SRS entirely (offline / no tokens)

Commands inside the text loop: /status  /prompt  /profile  /quit

The console rendering is `backend.terminal`, the page's buttons are `backend.page_control`, and
the wiring is `backend.orchestrator` (split out of this file on 2026-09-12).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from backend import config, orchestrator
from backend.orchestrator import stop_containers as _stop_containers  # noqa: F401 - kept for callers
from backend.status import registry
from backend.terminal import BOLD, DIM, RESET


def _first_run_notes(cfg, args) -> None:
    """What a student with nothing configured needs to be told, once, at the top of the launch.

    The settings page is the interface (ADR-022), so this points at it rather than at a file —
    and says what actually happens meanwhile, because a tutor that teaches blind without saying
    so is the confusing failure. A leftover `.env` is reported too: it is no longer read, and a
    key sitting in it that used to work would otherwise vanish silently (2026-09-12).
    """
    if stale := config.stale_dotenv():
        print(f"{BOLD}.env is no longer read{RESET} - atama-AI is configured in the settings page "
              f"now.\n  {len(stale)} key(s) are still in it: {', '.join(stale)}."
              f"\n  Import them once with: python -m backend.tools.migrate_env")
    if args.no_srs:
        return
    missing = [name for name, value in (("WaniKani", cfg.WANIKANI_TOKEN),
                                        ("Bunpro", cfg.BUNPRO_API_TOKEN)) if not str(value).strip()]
    if not missing:
        return
    where = "the settings page (it opens with the tutor) > Account"
    if len(missing) == 2:
        print(f"{BOLD}No study data yet.{RESET} Add your read-only API keys in {where}, and the "
              f"lesson is built from your own reviews.\n  Until then the tutor teaches as if you "
              f"were an early beginner, which works but is not the point.\n  Offline on purpose? "
              f"Start with --no-srs and this goes away.")
    else:
        print(f"{DIM}{missing[0]} is not connected - add its key in {where} for the rest of the "
              f"picture.{RESET}")


async def run(args: argparse.Namespace) -> int:
    cfg = config.load()
    print(f"{BOLD}atama-AI · {'voice lesson' if args.listen else 'text lesson'}{RESET}")
    _first_run_notes(cfg, args)

    lesson = orchestrator.Lesson(cfg, args)
    if code := await lesson.build():
        return code

    print(registry.table())
    print(f"{DIM}type Japanese and press enter · /status /prompt /profile /quit{RESET}\n")

    try:
        await lesson.open_lesson()
        if args.listen:
            await orchestrator.listen(lesson)
        else:
            await text_loop(lesson)
    finally:
        await lesson.close()
    return 0


async def text_loop(lesson: orchestrator.Lesson) -> None:
    """Type Japanese, read her back: the brain loop with no audio, plus the slash commands."""
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
            print(lesson.rendered.text)
            continue
        if line == "/profile":
            print(lesson.profile_text)
            continue
        await orchestrator.one_turn(lesson.brain, line, lesson.voice, mem=lesson.mem,
                                    coach=lesson.coach, noted=lesson.note_turn)


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    """Command line -> options. Separate from main() so `backend.tools.up` can reuse it rather
    than re-declaring flags that would then drift."""
    ap = argparse.ArgumentParser(prog="python -m backend.repl", description="atama-AI voice tutor")
    ap.add_argument("--refresh", action="store_true", help="force a fresh SRS sync")
    ap.add_argument("--no-srs", action="store_true", help="skip WaniKani/Bunpro entirely")
    ap.add_argument("--no-open", action="store_true", help="do not let Sensei speak first")
    ap.add_argument("--speak", action="store_true", help="speak each sentence aloud via VOICEVOX")
    ap.add_argument("--listen", action="store_true", help="talk to her: mic -> VAD -> Whisper (implies --speak)")
    ap.add_argument("--browser", action="store_true", help="send her voice to the browser avatar instead of this machine's speakers (implies --speak)")
    # NOT --open: `--no-open` above already means "do not let her greet you first", and a pair
    # that reads as each other's negation while meaning unrelated things is a trap.
    ap.add_argument("--show", action="store_true",
                    help="open the avatar page in your browser once the server is up")
    args = ap.parse_args(argv)
    args.speak = args.speak or args.listen or args.browser   # the help says implies; make it true
    return args


def main() -> int:
    try:
        return asyncio.run(run(parse()))
    except KeyboardInterrupt:
        return 130
    except config.ConfigError as exc:
        print(f"{BOLD}settings:{RESET} {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
