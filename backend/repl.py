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
from backend import memory as memory_api
from backend import vram as vram_mod
from backend import usage as usage_api
from backend.chunker import SentenceChunker
from backend.brain import BrainError, RateLimited, TextDelta, Thinking, ToolCall, ToolOutcome, TurnComplete
from backend.speaker import SpeechQueue
from backend.srs import profile as profile_api
from backend.tts_voicevox import VoicevoxClient, VoicevoxError
from backend.status import registry
from backend.tools import mcp_config

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
#: Not something the student said — the cue that a session has begun, so Sensei opens it (§5c).
#: Deliberately neutral: HOW to open (offer to continue last time, or find news) is tutor.md's.
OPENING_NUDGE = "（セッション開始。あいさつして、始めてください。）"
#: The page's New topic button. The same words tutor.md tells her to treat as 「話題を変えて」.
TOPIC_NUDGE = "（話題を変えて）"
#: After a live tutor change: the new person introduces themselves and carries on the lesson.
TUTOR_NUDGE = "（先生が交代しました。新しい先生として自己紹介して、レッスンを続けてください。）"


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

    # --- memory (spec §6b, ADR-031): catch up on past sessions, then read once --------------
    # Summarising happens HERE, at launch, for any session never summarised — not on exit, which
    # has to be instant. It is the init time the student already waits through for Whisper.
    mem, memory_text = None, ""
    if cfg.MEMORY_ENABLED:
        mem = memory_api.Memory.from_config(cfg)
        pending = mem.pending_logs()
        if pending:
            print(f"{DIM}memory: catching up on {len(pending)} past session(s) "
                  f"with {cfg.MEMORY_SUMMARY_MODEL}…{RESET}")
            landed = await _summarise(cfg, mem)
            print(f"{DIM}memory: {landed}/{len(pending)} summarised{RESET}")
        memory_text = mem.render()

    rendered = prompt.build(profile_text, persona=cfg.TUTOR_PERSONA, memory=memory_text)
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
    if mem is not None:
        mem.session_id = brain.session_id     # the turn log is named after the session it records
    try:
        await brain.start()
    except RuntimeError as exc:
        print(f"\n{BOLD}cannot start the brain:{RESET} {exc}\n", file=sys.stderr)
        return 2
    # --- mouth (spec §7): synthesis of sentence N+1 overlaps playback of N -------
    # --- the browser, if it is watching (spec §8) --------------------------------
    hub, server_task = None, None
    if args.browser:
        from backend import app as web
        hub = web.Hub()
        server_task, url = await web.serve(hub, cfg)
        print(f"{BOLD}avatar:{RESET} {url}")
        if getattr(args, "show", False):
            import webbrowser
            webbrowser.open(url)

    voice = None
    if args.speak:
        tts = VoicevoxClient.from_config(cfg)
        if tts.is_up():
            registry.report("voicevox", "loading", f"engine {tts.version} · speaker {tts.speaker}")
            for warning in tts.warnings:
                print(f"{DIM}voice: {warning}{RESET}")
            # With a browser attached the audio goes there, not to this machine — otherwise she
            # speaks twice, a few hundred milliseconds apart, which is worse than either alone.
            voice = SpeechQueue(tts, device=cfg.AUDIO_OUTPUT_DEVICE,
                                sink=(lambda s: hub.speak(s)) if hub else None)
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
    # The status bar (user request 2026-09-10): how full her context is, this month's use and the
    # rate-limit window — read after every turn from the brain's own last `result`.
    ledger = usage_api.Ledger(cfg.path("CACHE_DIR") / "usage")

    async def account(b) -> None:
        m = dict(getattr(b, "meters", {}) or {})
        month = await asyncio.to_thread(ledger.add_turn)
        rl = dict(getattr(b, "rate_limit", {}) or {})
        if hub is not None:
            await hub.meters(context_tokens=m.get("context_tokens"), context_window=m.get("context_window"),
                             month_turns=month["turns"],
                             limit_status=rl.get("status"), limit_type=rl.get("rateLimitType"),
                             limit_resets_at=rl.get("resetsAt"))

    if not args.no_open:
        await _one_turn(brain, OPENING_NUDGE, voice, mem=mem, opening=True)
        await account(brain)

    async def switch_persona(name: str):
        """Live tutor change (settings panel): a fresh session with the new persona, speaking in
        the voice that persona declares (ADR-030). Returns the started brain. The old one keeps
        answering until this one is up, so a failed switch leaves a tutor rather than nobody."""
        nonlocal brain
        fresh = config.load()
        text = prompt.build(profile_text, persona=name, memory=memory_text).text
        new = brain_api.create(fresh, registry=registry, mcp_config=mcp_json,
                               mcp_ready_markers=mcp_config.markers(fresh) if mcp_json else None,
                               system_prompt=text, allowed_tools=tools)
        await new.start()
        if voice is not None:
            tts_new = VoicevoxClient.from_config(fresh)
            try:
                await asyncio.to_thread(tts_new.warm_up)
            except VoicevoxError:
                pass                  # the first sentence pays for loading the style instead
            voice.tts = tts_new
        old, brain = brain, new
        if mem is not None:
            mem.session_id = new.session_id
        await old.aclose()
        return new

    if args.listen:
        try:
            await _listen(cfg, brain, voice, stt, hub, mem, switch_persona, account)
        finally:
            if voice is not None:
                await voice.aclose()
            await brain.aclose()
            if server_task is not None:
                from backend import app as web
                await web.shutdown(server_task)
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
            await _one_turn(brain, line, voice, mem=mem)
    finally:
        if voice is not None:
            await voice.aclose()
        await brain.aclose()
        if server_task is not None:
            from backend import app as web
            await web.shutdown(server_task)
    return 0


async def _load_stt(cfg):
    """Load and warm Whisper. Kicked off before the opening turn so its cost hides behind the
    tutor's first sentences instead of landing as unexplained silence after them."""
    from backend.stt import SpeechToText
    stt = SpeechToText.from_config(cfg)
    registry.report("stt", "loading", cfg.WHISPER_MODEL)
    # The model's own VRAM share (ROADMAP 11): the difference across its load, because Windows'
    # driver reports every per-process figure as [N/A] (backend/vram.py, verified 2026-09-10).
    before = await asyncio.to_thread(vram_mod.read)
    await asyncio.to_thread(stt.load)
    after = await asyncio.to_thread(vram_mod.read)
    share = (f" · VRAM {(after.used_mib - before.used_mib) / 1024:.1f} GB"
             if before is not None and after is not None else "")
    registry.report("stt", "warm",
                    f"{cfg.WHISPER_MODEL} · load {stt.load_ms / 1000:.1f}s · warm {stt.warmup_ms / 1000:.1f}s{share}")
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
    except Exception as exc:  # noqa: BLE001 - AudioUnavailable, or PortAudioError with no mic at all
        # Not fatal any more (spec §9): the voice loop waits for a microphone and picks one up
        # the moment it is plugged in. Aborting here meant relaunching for a USB cable.
        print(f"{BOLD}no microphone yet{RESET} ({type(exc).__name__}) - starting anyway; plug one "
              f"in and it will be picked up.", file=sys.stderr)
        return True
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


async def _listen(cfg, brain, voice, stt, hub=None, mem=None, switch_persona=None, account=None) -> None:
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
    meter_state = {"last": 0.0, "peak": 0.0, "sent": 0.0}
    #: The latest GPU memory sample (spec §10b), for the turn log. Filled by the heartbeat below.
    gpu: dict = {"mib": None}
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
        if hub is not None and now - meter_state["sent"] >= 0.1:   # the page's meter: 10 Hz is plenty
            meter_state["sent"] = now
            asyncio.get_running_loop().create_task(hub.level(peak, prob))
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

    def report_turn(t) -> None:
        # Spec §10 / ROADMAP 10: every turn's breakdown with the Claude stage taken apart, the
        # session's rolling p50/p90, a warning over LATENCY_WARN_S, and the same record to the
        # page as `timing`. Barged-in turns are recorded but kept out of the percentiles.
        from backend.tools.latency_run import percentile
        done = [x.voice_to_voice_ms() for x in loop.timings if not x.barged_in and x.voice_to_voice_ms()]
        t.session_p50_ms, t.session_p90_ms = percentile(done, 50), percentile(done, 90)
        v2v = t.voice_to_voice_ms()
        slow = bool(v2v) and v2v > float(cfg.LATENCY_WARN_S) * 1000
        print(f"  {BOLD if slow else DIM}stt {t.stt_ms:.0f}ms · first sentence {t.first_chunk_ms:.0f}ms "
              f"(ttft {t.ttft_ms or 0:.0f}ms" + (f", thinking {t.thinking_chars} chars" if t.thinking_chars else "")
              + f") · voice→voice {v2v:.0f}ms · turn {t.total_ms:.0f}ms"
              + (f" · session p50 {t.session_p50_ms / 1000:.2f}s p90 {t.session_p90_ms / 1000:.2f}s "
                 f"({len(done)} turns)" if t.session_p50_ms else "")
              + (f"  OVER {float(cfg.LATENCY_WARN_S):.1f}s" if slow else "") + f"{RESET}\n")
        if hub is not None:
            asyncio.get_running_loop().create_task(hub.timing(
                stt_ms=t.stt_ms, first_chunk_ms=t.first_chunk_ms, first_audio_ms=t.first_audio_ms,
                total_ms=t.total_ms, barged_in=t.barged_in, ttft_ms=t.ttft_ms,
                thinking_chars=t.thinking_chars, p50_ms=t.session_p50_ms, p90_ms=t.session_p90_ms,
                turns=len(done)))
        if account is not None:
            asyncio.get_running_loop().create_task(account(loop.brain))

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
        on_turn=report_turn,
    )
    loop_ref["loop"] = loop

    def device_changed(state: str, detail: str) -> None:
        # Unplugged, missing at launch, back again, or on the default instead of the chosen one:
        # say so on both screens, or a dead mic is indistinguishable from a quiet student.
        mark = BOLD if state in ("missing", "lost") else DIM
        print(chr(13) + mark + "[microphone " + state + "] " + detail + RESET + " " * 10, flush=True)
        if hub is not None:
            asyncio.get_running_loop().create_task(hub.status("microphone", state, detail))

    loop.on_device = device_changed

    from backend import settings_view
    from backend.device_watch import DeviceWatch

    def devices_changed(options: dict) -> None:
        # Plugged in or pulled out (spec §9): refresh the panel's pickers, and if we are on the
        # default only because the chosen mic was missing, go and get it.
        settings_view.latest = options
        loop.reopen_mic()
        if hub is not None:
            asyncio.get_running_loop().create_task(hub.push_settings())

    watch = DeviceWatch(devices_changed)
    await watch.start()

    async def watch_vram() -> None:
        # Spec §10b / ROADMAP 11: sampled on the status heartbeat, off the conversation path
        # (nvidia-smi runs in a thread), with a warning above the cap — said once per crossing.
        warned = False
        while True:
            reading = await asyncio.to_thread(vram_mod.read)
            if reading is not None:
                gpu["mib"] = reading.used_mib
                too_much = vram_mod.over(reading, float(cfg.VRAM_WARN_GB))
                if too_much and not warned:
                    print(chr(13) + BOLD + f"[gpu] {reading.used_gb:.1f} GB in use - over the "
                          f"{cfg.VRAM_WARN_GB} GB cap (spec §10b)" + RESET, flush=True)
                warned = too_much
                if hub is not None:
                    await hub.status("gpu", "over" if too_much else "ok",
                                     f"{reading.used_gb:.1f} of {reading.total_gb:.0f} GB")
                    await hub.meters(vram_used_mib=reading.used_mib, vram_total_mib=reading.total_mib)
            await asyncio.sleep(float(cfg.STATUS_HEARTBEAT_S))

    vram_task = asyncio.get_running_loop().create_task(watch_vram())

    async def change_tutor(name: str) -> None:
        # A different person is about to speak: stop the current one, bring up the new persona
        # and voice, then let them introduce themselves. Used to wait for the next launch, so the
        # panel's tutor cards seemed to do nothing (2026-09-10).
        await hub.status("tutor", "switching", f"switching to {name}...", remember=False)
        print(chr(13) + BOLD + "[tutor] switching to " + name + RESET + " " * 20, flush=True)
        if loop._turn_task is not None and not loop._turn_task.done():
            loop.voice.cancel()
            loop._turn_task.cancel()
        try:
            loop.brain = await switch_persona(name)
        except Exception as exc:  # noqa: BLE001 - the old tutor carries on rather than nobody
            print(chr(13) + BOLD + "[tutor] could not switch: " + f"{type(exc).__name__}: {exc}"
                  + RESET, flush=True)
            await hub.status("tutor", "failed", f"could not switch to {name} - "
                             f"{type(exc).__name__}", remember=False)
            return
        await hub.status("tutor", "ok", f"now teaching: {name}", remember=False)
        loop.ask(TUTOR_NUDGE)

    if hub is not None:
        def settings_saved(keys: list[str]) -> None:
            # settings_view.LIVE: devices and the tutor take effect now; the rest at next launch.
            fresh = config.load()
            if "AUDIO_INPUT_DEVICE" in keys:
                loop.reopen_mic(fresh.AUDIO_INPUT_DEVICE, changed=True)
            if "AUDIO_OUTPUT_DEVICE" in keys:
                voice.set_device(fresh.AUDIO_OUTPUT_DEVICE)
            if "TUTOR_PERSONA" in keys and switch_persona is not None:
                asyncio.get_running_loop().create_task(change_tutor(str(fresh.TUTOR_PERSONA)))

        hub.on_settings = settings_saved

    if mem is not None:
        # Record in the speaking gap: on_turn fires from _turn's finally, after TurnComplete, so
        # nothing here can sit between the student stopping and her first audio (spec §6b).
        printed = loop.on_turn

        def remember(t) -> None:
            if printed is not None:
                printed(t)
            mem.record_turn(student=t.transcript, tutor_sentences=list(t.sentences),
                            student_extra={"stt_ms": round(t.stt_ms)},
                            latency={"first_audio_ms": round(t.first_audio_ms),
                                     "voice_to_voice_ms": round(t.voice_to_voice_ms()),
                                     "ttft_ms": round(t.ttft_ms) if t.ttft_ms else None,
                                     "thinking_chars": t.thinking_chars,
                                     "barged_in": t.barged_in,
                                     # rolling over the session so far (spec §10)
                                     "session_p50_ms": round(t.session_p50_ms) if t.session_p50_ms else None,
                                     "session_p90_ms": round(t.session_p90_ms) if t.session_p90_ms else None,
                                     "vram_mib": gpu["mib"]})           # spec §10b, latest sample

        loop.on_turn = remember

    if hub is not None:
        # The browser is a better push-to-talk button than the terminal, because a browser can
        # see key RELEASE. `control: start`/`stop` are exactly the ptt edges (spec §8/§9), so
        # holding the key there gives real hold-to-talk instead of the toggle a TTY is limited to.
        # The terminal binding stays live as well — either can drive the same turn.
        def from_browser(action: str) -> None:
            # Report what the press actually produced. "recording" in the browser only proves the
            # message arrived; whether the microphone thread is feeding the buffer, and whether
            # the result was long enough to become a turn, are separate questions that look
            # identical from the UI (2026-09-10).
            # Visible on the console: a press that never arrives and a press that arrives but
            # produces no audio are different problems, and they look identical otherwise.
            print(chr(13) + DIM + "[browser: " + action + "]" + RESET + " " * 30, end="", flush=True)
            if action == "quit":
                # The page's stop button. Stopping the loop ends _listen, and run()'s finally
                # then closes the claude subprocess, the speech queue and this server in order.
                print(chr(13) + BOLD + "stop requested from the page - shutting down" + RESET, flush=True)
                loop.stop()
                return
            import numpy as np
            from backend import audio as audio_mod

            def ack(state: str, detail: str) -> None:
                # Every press gets an answer the page can show. A press that is never answered
                # is how the page recognises a dead link, and it then reconnects (2026-09-10:
                # presses were vanishing and the page could not tell anyone).
                asyncio.get_running_loop().create_task(
                    hub.status("ptt", state, detail, remember=False))

            if action == "new_topic":
                loop.ask(TOPIC_NUDGE)
                ack("topic", "finding a new topic...")
                return
            if action == "start":
                if not loop.ptt:
                    ack("off", "hands-free mode is on - just speak")
                    return
                loop.ptt_begin()
                ack("recording", "listening - release to send")
            elif action == "stop":
                buf = list(loop._ptt_buf)
                frames = len(buf)
                seconds = frames * 512 / 16000
                rms = float(np.sqrt(np.mean(np.square(np.concatenate(buf))))) if buf else 0.0
                before = loop._turn_task
                loop.ptt_end()
                started = loop._turn_task is not None and loop._turn_task is not before
                print(chr(13) + DIM + "[browser: stop] " + str(frames) + " frames ("
                      + format(seconds, ".1f") + "s, rms " + format(rms, ".5f") + ") -> "
                      + ("turn started" if started else "NO TURN")
                      + "  mic_alive=" + str(loop._mic.is_alive() if loop._mic else False)
                      + " frames_seen=" + str(loop.frames_seen)
                      + (" err=" + loop.capture_error if loop.capture_error else "")
                      + RESET + " " * 10, flush=True)
                if not loop.ptt:
                    return
                if frames == 0:
                    ack("empty", "no audio arrived from the microphone")
                elif rms < audio_mod.SILENT_RMS:
                    # Digital silence: the device is open but muted (boom up, mute button, or
                    # Windows privacy). Sending it anyway only earns a discarded hallucination.
                    ack("silent", f"your microphone sent silence ({seconds:.1f}s) - is it muted?")
                elif started:
                    ack("sent", f"sent {seconds:.1f}s - she is listening to it")
                elif seconds * 1000 < loop.vad.min_speech_ms:
                    ack("short", "too short to send - hold SPACE while you speak")
                else:
                    ack("busy", "she is still working on your last turn")
                return

        hub.on_control = from_browser

        # The browser was getting audio and nothing else: no transcript, no state. Silence after
        # a press then looked the same as a broken microphone, when it might equally be a
        # discarded transcript or the brain still thinking. These are cheap and they are the
        # difference between "it is not working" and "it did not hear me".
        def tell_browser(coro) -> None:
            asyncio.get_running_loop().create_task(coro)

        loop.on_state = lambda s: (
            print(chr(13) + DIM + "[" + s + "]" + RESET + " " * 50, end="", flush=True)
            if s != "listening" else None,
            tell_browser(hub.state(s)))[0]
        loop.on_transcript = lambda t: (
            print(chr(13) + BOLD + "you:" + RESET + " " + t.text if t
                  else chr(13) + DIM + "(discarded: " + t.reason + ")" + RESET),
            tell_browser(hub.transcript(t.text, bool(t), getattr(t, "reason", ""))))[0]

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
        await watch.close()
        vram_task.cancel()
        if loop.timings:
            v2v = sorted(t.voice_to_voice_ms() for t in loop.timings if t.voice_to_voice_ms())
            if v2v:
                p90 = v2v[max(0, int(len(v2v) * 0.9) - 1)]
                print(f"\n{DIM}{len(v2v)} turns · voice→voice median {v2v[len(v2v) // 2] / 1000:.2f}s · "
                      f"p90 {p90 / 1000:.2f}s (budget 5.0s){RESET}")


async def _summarise(cfg, mem) -> int:
    """One short-lived, cheap brain that summarises past sessions, then goes away.

    Same Brain interface as the tutor (ADR-027) but its own process, its own model and no tools:
    it must not share the tutor's session, or the summary request would sit in her transcript.
    Every failure path returns 0 — no summary means no recall of that session, not no lesson.
    """
    instructions = (prompt.PROMPTS_DIR / "summarise.md").read_text(encoding="utf-8")
    worker = brain_api.create(cfg, registry=None, allowed_tools=(),
                              model=str(cfg.MEMORY_SUMMARY_MODEL),
                              system_prompt="You summarise language lessons. Reply with one JSON object only.")
    try:
        await asyncio.wait_for(worker.start(), 90)
    except Exception as exc:  # noqa: BLE001 - memory is best-effort by contract
        print(f"{DIM}memory: summariser did not start ({type(exc).__name__}); skipping{RESET}")
        return 0

    async def ask(text: str) -> str:
        out: list[str] = []
        async for ev in worker.turn(text):
            if isinstance(ev, TextDelta):
                out.append(ev.text)
            elif isinstance(ev, TurnComplete):
                break
        return "".join(out)

    try:
        return await asyncio.wait_for(mem.summarise_pending(ask, instructions), 240)
    except Exception:  # noqa: BLE001
        return 0
    finally:
        await worker.aclose()


async def _one_turn(brain, text: str, voice=None, mem=None, opening: bool = False) -> None:
    """Send one turn; print each sentence the moment it closes, with its latency."""
    chunker = SentenceChunker()
    spoken: list[dict] = []
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
            spoken.append({"text": chunk.text, "emotion": chunk.emotion or None, "synth_ms": None})
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
            if mem is not None:
                # The opening nudge is our cue, not something the student said — log it empty,
                # but keep her opener: it is exactly the topic the next session must not repeat.
                mem.record_turn(student="" if opening else text, tutor_sentences=spoken,
                                latency={"ttft_ms": round(event.ttft_ms) if event.ttft_ms else None},
                                usage=event.usage or {})
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


if __name__ == "__main__":
    sys.exit(main())
