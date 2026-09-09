"""M1 text REPL — the brain loop with no audio (spec §12 M1).

Type Japanese, watch sentence chunks come back with their emotion and their timings. This is
the milestone's demoable artefact and the place where the first-chunk latency of §10 becomes
visible: the first sentence should print well before the turn finishes.

    python -m backend.repl            # launch: sync SRS, render prompt, start the brain
    python -m backend.repl --speak    # ...and speak every sentence aloud through VOICEVOX
    python -m backend.repl --listen --speak   # ...and talk to her out loud (full voice loop)
    python -m backend.repl --refresh  # force a fresh SRS sync (spec §5 manual Refresh)
    python -m backend.repl --no-srs   # skip SRS entirely (offline / no tokens)

Commands inside the loop: /status  /prompt  /profile  /quit
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import threading
import sys
import time

from backend import brain as brain_api
from backend import config, prompt
from backend.chunker import SentenceChunker
from backend.brain import BrainError, RateLimited, TextDelta, Thinking, ToolCall, ToolOutcome, TurnComplete
from backend.speaker import SpeechQueue
from backend.srs import profile as profile_api
from backend.tts_voicevox import VoicevoxClient, VoicevoxError
from backend.status import registry
from backend.tools import mcp_config

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
#: Not something the student said — the cue that a session has begun, so Sensei opens it (§5c).
OPENING_NUDGE = "（セッション開始。あいさつして、話題を一つ見つけて、質問してください。）"


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
    rendered = prompt.build(profile_text, persona=cfg.TUTOR_PERSONA)
    profile_api.debug_snapshot(student, profile_text, cfg.path("LOG_DIR"), dt.date.today().isoformat())
    sections = " · ".join(f"{k} {v}" for k, v in rendered.sections.items())
    print(f"{DIM}prompt {rendered.tokens} tokens ({sections})"
          f"{' · TRUNCATED: ' + ', '.join(rendered.truncated) if rendered.truncated else ''}{RESET}")

    # --- brain (ADR-027: the REPL talks to the interface, not to Claude) --------
    tools = mcp_config.allowed_tools(cfg)
    mcp_json = mcp_config.write(cfg) if tools else None
    brain = brain_api.create(
        cfg, registry=registry,
        mcp_config=mcp_json,
        mcp_ready_markers=mcp_config.markers(cfg) if mcp_json else None,
        system_prompt=rendered.text,
        allowed_tools=tools,
    )
    try:
        await brain.start()
    except RuntimeError as exc:
        print(f"\n{BOLD}cannot start the brain:{RESET} {exc}\n", file=sys.stderr)
        return 2
    # --- mouth (spec §7): synthesis of sentence N+1 overlaps playback of N -------
    voice = None
    if args.speak:
        tts = VoicevoxClient.from_config(cfg)
        if tts.is_up():
            registry.report("voicevox", "loading", f"engine {tts.version} · speaker {tts.speaker}")
            for warning in tts.warnings:
                print(f"{DIM}voice: {warning}{RESET}")
            voice = SpeechQueue(tts, device=cfg.AUDIO_OUTPUT_DEVICE)
            await voice.start()
            # The engine loads a style's model on first use, so answering /version is not the
            # same as being able to speak. Pay that here rather than inside Sensei's opening
            # line: otherwise the student's first experience is silence they cannot read as
            # either warming up or broken (spec §5b, constants: VOICEVOX_INIT_SPEAKER).
            try:
                loaded, warm_ms = await asyncio.to_thread(tts.warm_up)
                registry.report("voicevox", "warm",
                                f"engine {tts.version} · speaker {tts.speaker} · "
                                f"{loaded} style(s) · warm {warm_ms / 1000:.1f}s")
                print(f"{DIM}voice ready: {loaded} style(s) loaded in {warm_ms / 1000:.1f}s{RESET}")
            except VoicevoxError as exc:
                # The engine is up but would not preload; let her talk and let the first
                # sentence pay for it, rather than refusing to run.
                registry.report("voicevox", "ok",
                                f"engine {tts.version} · speaker {tts.speaker} · not preloaded",
                                last_error=str(exc))
                print(f"{DIM}voice: could not preload styles; the first sentence may lag{RESET}")
        else:
            registry.report("voicevox", "down", f"no engine at {cfg.VOICEVOX_URL}")
            print(f"{BOLD}VOICEVOX is not running{RESET} — `docker compose up -d voicevox`. Continuing in text only.")

    # Everything the conversation needs is loaded HERE, before the tutor says a word. Whisper
    # used to load after the opening turn, which left the student watching him finish and then
    # sitting in silence while the mic came alive (reported 2026-09-09). Racing the load against
    # his sentences would hide the cost but keep "am I being heard yet?" ambiguous, so we pay it
    # up front: when the status table appears, every subsystem is genuinely ready.
    stt = None
    if args.listen and voice is not None:
        print(f"{DIM}loading {cfg.WHISPER_MODEL} ({cfg.WHISPER_COMPUTE_TYPE})… "
              f"this is the slow part of startup{RESET}")
        try:
            stt = await _load_stt(cfg)
            print(f"{DIM}whisper ready: load {stt.load_ms / 1000:.1f}s, "
                  f"warm-up {stt.warmup_ms / 1000:.1f}s{RESET}")
        except Exception as exc:  # noqa: BLE001 - a load failure must be legible, not a traceback
            registry.report("stt", "error", cfg.WHISPER_MODEL, f"{type(exc).__name__}: {exc}")
            print(f"{BOLD}cannot load Whisper:{RESET} {exc}", file=sys.stderr)
            if voice is not None:
                await voice.aclose()
            await brain.aclose()
            return 2
        if not await _check_microphone(cfg):
            if voice is not None:
                await voice.aclose()
            await brain.aclose()
            return 2

    print(registry.table())
    print(f"{DIM}type Japanese and press enter · /status /prompt /profile /quit{RESET}\n")

    # Sensei speaks first (spec §5c): she finds a subject and opens on it, rather than waiting
    # for the student to produce one. This is the turn that pays for the search.
    if not args.no_open:
        await _one_turn(brain, OPENING_NUDGE, voice)

    if args.listen:
        try:
            await _listen(cfg, brain, voice, stt)
        finally:
            if voice is not None:
                await voice.aclose()
            await brain.aclose()
        return 0

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
            await _one_turn(brain, line, voice)
    finally:
        if voice is not None:
            await voice.aclose()
        await brain.aclose()
    return 0


async def _load_stt(cfg):
    """Load and warm Whisper. Kicked off before the opening turn so its cost hides behind the
    tutor's first sentences instead of landing as unexplained silence after them."""
    from backend.stt import SpeechToText
    stt = SpeechToText.from_config(cfg)
    registry.report("stt", "loading", cfg.WHISPER_MODEL)
    await asyncio.to_thread(stt.load)
    registry.report("stt", "warm",
                    f"{cfg.WHISPER_MODEL} · load {stt.load_ms / 1000:.1f}s · warm {stt.warmup_ms / 1000:.1f}s")
    return stt


def _ptt_keys(loop, aloop):
    """SPACE opens and closes the turn, pumped from a daemon thread.

    A terminal cannot see key RELEASE, only presses, so this is a toggle rather than a true
    hold. In the browser (M3) it becomes a real hold on mousedown/mouseup — the loop API is the
    same either way, which is why ptt_begin/ptt_end are two calls and not one.
    """
    stop = threading.Event()

    def toggle():
        loop.ptt_end() if loop._ptt_open else loop.ptt_begin()

    def pump():
        try:
            import msvcrt
        except ImportError:                      # POSIX: no raw keys, ENTER toggles instead
            while not stop.is_set():
                if sys.stdin.readline() == "":
                    return
                aloop.call_soon_threadsafe(toggle)
            return
        while not stop.is_set():
            if msvcrt.kbhit():
                if msvcrt.getch() in (b" ", b"\r"):
                    aloop.call_soon_threadsafe(toggle)
            else:
                time.sleep(0.01)

    threading.Thread(target=pump, name="ptt-keys", daemon=True).start()
    return stop


async def _check_microphone(cfg) -> bool:
    """Prove the mic delivers audio BEFORE the conversation starts. Returns False to abort.

    A mic that opens but delivers digital silence is the most likely failure here, and it looks
    exactly like "the app is broken" if we say nothing (found while testing, 2026-09-09).
    """
    from backend import audio as audio_mod
    try:
        level = await asyncio.to_thread(audio_mod.input_level, cfg.AUDIO_INPUT_DEVICE, 1.0)
    except audio_mod.AudioUnavailable as exc:
        print(f"{BOLD}no microphone:{RESET} {exc}", file=sys.stderr)
        return False
    device = cfg.AUDIO_INPUT_DEVICE or "system default"
    if level < audio_mod.SILENT_RMS:
        print(f"\n{BOLD}The microphone ({device}) looks muted{RESET} — rms {level:.6f} over one second, "
              f"which is digital silence rather than a quiet room. It opened, so the device exists.\n"
              f"  1. the physical mute on the headset (on many, flipping the boom up mutes it)\n"
              f"  2. Settings > Privacy & security > Microphone > 'Let desktop apps access your microphone'\n"
              f"  3. Settings > System > Sound > Input > device level is not 0\n"
              f"  4. `python -m backend.audio` — a device listed only under WDM-KS is not selectable\n"
              f"Starting anyway — the meter will show whether you are being heard.\n")
    else:
        print(f"{DIM}microphone ready: {device} · rms {level:.4f}{RESET}")
    return True


async def _listen(cfg, brain, voice, stt) -> None:
    """Full voice loop: speak to him, he answers aloud (spec §2).

    Everything is already loaded by the time this runs — see the init block in `run()`.
    """
    from backend.vad import VoiceActivityDetector
    from backend.voice_loop import VoiceLoop

    if voice is None or stt is None:
        # --listen implies --speak, so getting here means the mouth failed to open, not that the
        # user forgot a flag. Say which, or they go hunting through argv for a problem that is in
        # Docker.
        print(f"{BOLD}cannot listen without a voice{RESET} — VOICEVOX is not answering at "
              f"{cfg.VOICEVOX_URL}. Start it with `docker compose up -d voicevox` and try again.",
              file=sys.stderr)
        return

    vad = VoiceActivityDetector.from_config(cfg)

    # A live meter on the prompt line: the difference between "it is not hearing me" and
    # "it heard me and decided that was not speech" should never be a guess.
    meter_state = {"last": 0.0, "peak": 0.0}
    #: The meter needs the loop to show push-to-talk state, and the loop is built below.
    loop_ref: dict = {"loop": None}

    def show_level(level: float, prob: float) -> None:
        meter_state["peak"] = max(meter_state["peak"], level)   # peak-hold between redraws
        now = time.monotonic()
        if now - meter_state["last"] < 0.05:                    # 20 fps: responsive, not flickery
            return
        meter_state["last"] = now
        peak = meter_state["peak"]
        meter_state["peak"] = peak * 0.45                       # ~decays to nothing in 0.2 s
        # Quiet mics are the norm here (this one peaks around 0.02 on speech), so scale by the
        # square root: a linear bar on a 0-1 range barely twitches and reads as "not hearing you".
        bars = int(min(1.0, (peak * 30) ** 0.5) * 28)
        hot = prob >= vad.active_threshold
        live = loop_ref["loop"]
        if live is not None and live.ptt:
            label = "RECORDING   " if live._ptt_open else "SPACE to talk"
            colour = BOLD if live._ptt_open else DIM
        else:
            label = "HEARING YOU" if hot else "listening   "
            colour = BOLD if hot else DIM
        print(f"\r{colour}{label}{RESET} |{('#' * bars):<28}| {DIM}{prob:.2f}{RESET}  ", end="", flush=True)

    loop = VoiceLoop(
        turn_mode=cfg.TURN_MODE,
        brain=brain, stt=stt, vad=vad, voice=voice, input_device=cfg.AUDIO_INPUT_DEVICE,
        on_level=show_level,
        on_state=lambda s: print(f"\r{DIM}[{s}]{RESET}" + " " * 50, end="", flush=True) if s != "listening" else None,
        on_transcript=lambda t: print(
            f"\r{BOLD}you:{RESET} {t.text}" if t
            else f"\r{DIM}(discarded: {t.reason} — {t.text[:40]}){RESET}"),
        on_chunk=lambda c, ms: print(f"  {DIM}{ms / 1000:5.2f}s{RESET} {_emotion_tag(c.emotion)}{c.text}"),
        on_bargein=lambda: print(f"\r{BOLD}— interrupted —{RESET}"),
        on_turn=lambda t: print(f"  {DIM}stt {t.stt_ms:.0f}ms · first sentence {t.first_chunk_ms:.0f}ms · "
                                f"voice→voice {t.voice_to_voice_ms():.0f}ms · turn {t.total_ms:.0f}ms{RESET}\n"),
    )
    loop_ref["loop"] = loop
    device = cfg.AUDIO_INPUT_DEVICE or "system default"
    keys = _ptt_keys(loop, asyncio.get_running_loop()) if loop.ptt else None
    if loop.ptt:
        print(f"\n{BOLD}Push to talk on {device}.{RESET} {BOLD}SPACE{RESET} to start speaking, "
              f"{BOLD}SPACE{RESET} again when done. Press while he is talking to interrupt. "
              f"Ctrl+C to stop.\n")
    else:
        print(f"\n{BOLD}Listening on {device}.{RESET} Speak Japanese. Ctrl+C to stop.\n")
    try:
        await loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        loop.stop()
        if loop.timings:
            v2v = sorted(t.voice_to_voice_ms() for t in loop.timings if t.voice_to_voice_ms())
            if v2v:
                p90 = v2v[max(0, int(len(v2v) * 0.9) - 1)]
                print(f"\n{DIM}{len(v2v)} turns · voice→voice median {v2v[len(v2v) // 2] / 1000:.2f}s · "
                      f"p90 {p90 / 1000:.2f}s (budget 3.0s){RESET}")


async def _one_turn(brain, text: str, voice=None) -> None:
    """Send one turn; print each sentence the moment it closes, with its latency."""
    chunker = SentenceChunker()
    started = time.monotonic()
    first_chunk_at: float | None = None
    n = 0
    thinking_chars = 0

    async def show(chunks) -> None:
        nonlocal first_chunk_at, n
        for chunk in chunks:
            now = time.monotonic() - started
            first_chunk_at = first_chunk_at if first_chunk_at is not None else now
            n += 1
            print(f"  {DIM}{now:5.2f}s{RESET} {_emotion_tag(chunk.emotion)}{chunk.text}")
            if voice is not None:
                await voice.say(chunk)

    async for event in brain.turn(text):
        if isinstance(event, TextDelta):
            await show(chunker.push(event.text))
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
            await show(chunker.close())
            if voice is not None:
                await voice.drain()
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
    ap.add_argument("--no-open", action="store_true", help="do not let Sensei speak first")
    ap.add_argument("--speak", action="store_true", help="speak each sentence aloud via VOICEVOX")
    ap.add_argument("--listen", action="store_true", help="talk to her: mic -> VAD -> Whisper (implies --speak)")
    args = ap.parse_args()
    args.speak = args.speak or args.listen      # the help says implies; make it true (2026-09-09)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
