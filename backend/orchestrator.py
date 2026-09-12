"""Building the lesson: brain, mouth, ears, memory and the page, wired into one session.

`Lesson.build()` is the launch in order (SRS snapshot -> page -> voice -> Whisper -> memory ->
prompt -> brain); `listen()` runs the voice conversation on top of it; `Lesson.close()` takes it
all down. The session factory (`spawn_rotation`, ADR-032), Refresh (`resync`, ADR-024), the live
tutor change (`switch_persona`, ADR-030) and the summariser (`summarise`, ADR-031) live here too.
Split out of repl.py on 2026-09-12; every dated finding moved with its code.

Console output goes through `backend.terminal`; everything the page is told goes through
`backend.page_control`, which sanitises it (spec §11).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
import time
from typing import Any, Callable

from backend import annotate as annotate_api
from backend import brain as brain_api
from backend import config, page_control, prompt, terminal
from backend import explain as explain_api
from backend import memory as memory_api
from backend import model_tiers
from backend import session as session_api
from backend import study as study_api
from backend import study_plan
from backend import usage as usage_api
from backend import vram as vram_mod
from backend.brain import (BrainError, Compacting, RateLimited, TextDelta, Thinking, ToolCall, ToolOutcome,
                           TurnComplete)
from backend.chunker import SentenceChunker
from backend.speaker import SpeechQueue
from backend.srs import profile as profile_api
from backend.status import registry
from backend.terminal import BOLD, DIM, RESET
from backend.tools import mcp_config
from backend.tools.latency_run import percentile
from backend.tts_voicevox import VoicevoxClient, VoicevoxError

#: Not something the student said — the cue that a session has begun, so Sensei opens it (§5c).
#: Deliberately neutral: HOW to open (offer to continue last time, or find news) is tutor.md's.
OPENING_NUDGE = "（セッション開始。あいさつして、始めてください。）"
#: How much of the lesson a rotated session inherits (ADR-032), newest kept. The excerpt is the
#: newest characters of the turn log; prompt.build then keeps the newest LINES that fit
#: HANDOFF_MAX_TOKENS. Japanese estimates at ~1 token per character, so twice the budget fills it
#: with mixed text and can never leave it short — over-fetching is free because the tail is kept.
HANDOFF_CHARS = 2 * prompt.HANDOFF_MAX_TOKENS
#: Manual Refresh (spec §5b "still rate-limited"): at most one SRS re-fetch a minute.
RESYNC_MIN_S = 60.0
#: How long the launch waits for the last lesson's summary once everything else is warm. It runs
#: from the top of the launch, so this is only what is LEFT of it; the same summary has taken 13.7 s
#: and 52.6 s, so the cap is generous. Past it she starts anyway and the summary lands for the next
#: launch — a tutor that never appears is worse than one a lesson behind (user, 2026-09-12).
MEMORY_WAIT_S = 45.0


def sanitised(value: Any, sanitize=None) -> Any:
    """Every string inside a tool record (name, args, error) through the registry's sanitiser
    before it is written to the turn log — spec §11 names tool-call args as a leak path."""
    clean = sanitize or registry.sanitize
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        return {k: sanitised(v, clean) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitised(v, clean) for v in value]
    return value


class Lesson:
    """Everything one launch builds and one exit closes. `build()` then `listen(lesson)` or the
    text loop, then `close()` — in `finally`, whatever happened."""

    def __init__(self, cfg: config.Config, args: argparse.Namespace) -> None:
        self.cfg = cfg
        self.args = args
        for secret in cfg.secrets().values():
            registry.register_secret(secret)
        self.mem = memory_api.Memory.from_config(cfg) if cfg.MEMORY_ENABLED else None   # per tutor
        self.student = profile_api.StudentProfile()
        self.profile_text = ""
        self.memory_text = ""
        #: Today's targets (spec §6c, ADR-038): built once the profile and memory are in, rendered
        #: into every session of this launch, advanced after every turn. Zero model calls.
        self.plan: study_plan.Plan | None = None
        self.plan_text = ""
        self.annotator: annotate_api.Annotator | None = None
        self.explainer = explain_api.Explainer(cfg)
        self.hub = None
        self.server_task: asyncio.Task | None = None
        self.voice: SpeechQueue | None = None
        self.stt = None
        self.brain = None
        self.rendered = None
        self.summary: asyncio.Task | None = None
        self.catchup: asyncio.Task | None = None
        self.ledger = usage_api.Ledger(cfg.path("CACHE_DIR") / "usage")
        self.last_sync = 0.0
        self._tiers = None
        self._tools: tuple = ()
        self._mcp_json = None

    # ------------------------------------------------------------------ the launch
    async def build(self) -> int:
        """Bring everything up, in the order that keeps the student from waiting twice. Returns 0,
        or an exit code when something that must exist could not be built."""
        cfg = self.cfg
        await self._sync_srs()
        # Furigana for the chat (spec §8b): a local tokenizer, and which kanji WaniKani says are passed.
        self.annotator = annotate_api.Annotator(
            known_kanji=self.student.wanikani.known_kanji if self.student.wanikani else (),
            kanji_readings=self.student.wanikani.kanji_readings if self.student.wanikani else {})

        # --- memory (spec §6b, ADR-031) ------------------------------------------------------
        # The summary of the last lesson is what she greets with, so she must have read it before she
        # says a word — but it is a model call whose latency is variance, not work (13.7 s and 52.6 s
        # on two runs of the same summary, 2026-09-12). So it starts HERE, at the top of the launch,
        # and runs while Whisper loads, VOICEVOX warms and the page comes up; it is awaited at the
        # bottom, just before her prompt is built. Nothing waits twice for the same seconds.
        if self.mem is not None and self.mem.pending_logs():
            print(f"{DIM}memory: reading back your last lesson ({cfg.MEMORY_SUMMARY_MODEL}) while "
                  f"everything loads…{RESET}", flush=True)
            self.summary = asyncio.create_task(summarise(cfg, self.mem, limit=1))

        if self.args.browser:
            await self._open_page()
        if self.args.speak:
            await self._open_voice()
        if self.args.listen and self.voice is not None and not await self._load_listening():
            return 2
        await self._read_memory()
        return await self._start_brain()

    async def _sync_srs(self) -> None:
        # --- launch: SRS snapshot -> student profile (spec §5, ADR-024) -------------
        cfg = self.cfg
        if self.args.no_srs:
            self.student = profile_api.StudentProfile()
            print(f"{DIM}SRS skipped (--no-srs){RESET}")
        else:
            t0 = time.monotonic()
            self.student = await asyncio.to_thread(
                profile_api.build,
                cfg.WANIKANI_TOKEN, cfg.BUNPRO_API_TOKEN,
                cfg.path("CACHE_DIR") / "srs", cfg.SRS_CACHE_TTL_S, cfg.SRS_FETCH_BUDGET_S,
                registry, force=self.args.refresh,
            )
            print(f"{DIM}SRS sync {time.monotonic() - t0:.1f}s{RESET}")
        self.profile_text = profile_api.render(self.student)

    async def _open_page(self) -> None:
        # --- the browser, if it is watching (spec §8) --------------------------------
        from backend import app as web
        self.hub = hub = web.Hub()
        self.server_task, url = await web.serve(hub, self.cfg, registry)   # status chips follow the registry
        hub.explain = self.explainer.explain
        if await asyncio.to_thread(self.annotator.warm):
            hub.readings = self.annotator.readings
            hub.grammar = self.annotator.grammar_only
            # The grammar safety net: a point of theirs she used without marking it - 「と思います」
            # stayed black on the page (user, 2026-09-12) - found by the tokenizer's base forms.
            hub.points = self.annotator.find_points
        else:
            print(f"{DIM}no furigana in the chat: {self.annotator.error}{RESET}")
        # Their own words, blue in the chat (spec §8b): from the snapshot already fetched, never
        # a new call (ADR-024). Rebuilt by the Refresh button, below.
        hub.study = study_api.Study.from_profile(self.student, getattr(self.annotator, 'tokens', None))
        print(f"{BOLD}avatar:{RESET} {url}")
        if getattr(self.args, "show", False):
            import webbrowser
            webbrowser.open(url)

    async def _open_voice(self) -> None:
        # --- mouth (spec §7): synthesis of sentence N+1 overlaps playback of N -------
        cfg, hub = self.cfg, self.hub
        tts = await VoicevoxClient.from_config_async(cfg)
        if not tts.is_up():
            registry.report("voicevox", "down", f"no engine at {cfg.VOICEVOX_URL}")
            print(f"{BOLD}VOICEVOX is not running{RESET} — `docker compose up -d voicevox`. Continuing in text only.")
            return
        registry.report("voicevox", "loading", f"engine {tts.version} · speaker {tts.speaker}")
        for warning in tts.warnings:
            print(f"{DIM}voice: {warning}{RESET}")
        # With a browser attached the audio goes there, not to this machine — otherwise she
        # speaks twice, a few hundred milliseconds apart, which is worse than either alone.
        self.voice = SpeechQueue(tts, device=cfg.AUDIO_OUTPUT_DEVICE,
                                 sink=(lambda s: hub.speak(s)) if hub else None)
        await self.voice.start()
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

    async def _load_listening(self) -> bool:
        # Everything the conversation needs is loaded HERE, before the tutor says a word. Whisper
        # used to load after the opening turn, which left the student watching him finish and then
        # sitting in silence while the mic came alive (reported 2026-09-09). Racing the load against
        # his sentences would hide the cost but keep "am I being heard yet?" ambiguous, so we pay it
        # up front: when the status table appears, every subsystem is genuinely ready.
        cfg = self.cfg
        print(f"{DIM}loading {cfg.WHISPER_MODEL} ({cfg.WHISPER_COMPUTE_TYPE})… "
              f"this is the slow part of startup{RESET}")
        try:
            self.stt = await load_stt(cfg)
            print(f"{DIM}whisper ready: load {self.stt.load_ms / 1000:.1f}s, "
                  f"warm-up {self.stt.warmup_ms / 1000:.1f}s{RESET}")
        except Exception as exc:  # noqa: BLE001 - a load failure must be legible, not a traceback
            registry.report("stt", "error", cfg.WHISPER_MODEL, f"{type(exc).__name__}: {exc}")
            print(f"{BOLD}cannot load Whisper:{RESET} {exc}", file=sys.stderr)
            # Nothing has been spawned yet: the tutor is built after this, deliberately, so a
            # failed load costs a launch and not a live session.
            await self._abort()
            return False
        if not await check_microphone(cfg):
            await self._abort()
            return False
        return True

    async def _abort(self) -> None:
        """A launch that stops before the brain exists: close what was opened."""
        if self.voice is not None:
            await self.voice.aclose()
        if self.summary is not None:
            self.summary.cancel()

    async def _read_memory(self) -> None:
        # --- her mind, built last (2026-09-12) -------------------------------------------------
        # Everything else is warm by now, and the summary that started at the top of the launch has
        # had all of it to finish in. She reads it, THEN her prompt is assembled, THEN she is spawned:
        # a tutor who greets you having forgotten yesterday is what this ordering prevents.
        if self.summary is not None:
            # Whatever is left of it is waited for HERE, and it is the only thing left to wait for, so
            # say so: an unexplained pause after "whisper ready" looks like a hang (user, 2026-09-12).
            # It is also capped — past MEMORY_WAIT_S she carries on and the summary lands for the next
            # launch, because a tutor that never appears is worse than one a lesson behind.
            registry.report("brain", "starting", "reading back your last lesson")
            landed, waited = 0, time.monotonic()
            try:
                landed = await asyncio.wait_for(asyncio.shield(self.summary), MEMORY_WAIT_S)
            except asyncio.TimeoutError:
                print(f"{DIM}memory: the last lesson is taking longer than {MEMORY_WAIT_S:.0f}s — "
                      f"starting without it; it will be there next time{RESET}")
            except Exception as exc:  # noqa: BLE001 - memory is best-effort (ADR-031); a summariser
                print(f"{DIM}memory: could not read the last lesson back ({type(exc).__name__}); "
                      f"carrying on without it{RESET}")
            else:
                rest = len(self.mem.pending_logs())
                print(f"{DIM}memory: {'your last lesson is in' if landed else 'nothing new to remember'}"
                      f" ({time.monotonic() - waited:.0f}s)"
                      + (f"; {rest} older session(s) will follow in the background{RESET}" if rest else RESET))
        if self.mem is not None:
            self.memory_text = self.mem.render()

    def _make_plan(self) -> None:
        """Today's targets from the snapshot already in memory and the turn logs already on disk
        (spec §6c): folded here, at launch, in milliseconds — never a fetch, never a model call."""
        cfg = self.cfg
        items = study_api.Study.from_profile(self.student, getattr(self.annotator, 'tokens', None)).items
        spacing = dict(progress_after=int(cfg.STUDY_PROGRESS_AFTER), spacing_base=int(cfg.STUDY_SPACING_BASE),
                       spacing_max=int(cfg.STUDY_SPACING_MAX))
        ledger = study_plan.load_ledger(memory_api.sessions_dir(cfg), items, **spacing)
        recent = self.mem.recent_topics() if self.mem is not None else []
        facts = self.mem.facts()[0] if self.mem is not None else []
        index = self.mem.sessions_summarised() if self.mem is not None else ledger.today
        self.plan = study_plan.build(items, ledger, cfg, session_index=index, recent_topics=recent, facts=facts)
        self.plan_text = study_plan.render(self.plan)
        print(terminal.targets_line([t.item.text for t in self.plan.vocab], [t.item.text for t in self.plan.grammar],
                                    self.plan.opener.kind, self.plan.opener.subject))

    def coach(self, text: str) -> str:
        """The student's words with the coach note above them, when one is due (spec §6c). What
        the brain is asked — never what is logged, shown or spoken."""
        return study_plan.coached(self.plan, text)

    def note_turn(self, student: str, sentences: list[dict[str, Any]]) -> None:
        """After every turn, the same facts memory recorded: the plan counts them and rotates."""
        if self.plan is None:
            return
        change = self.plan.note_turn({"student": {"text": student}, "tutor": {"sentences": sentences}})
        for (old, gap), new in zip(change.retired, [*change.promoted, *([None] * len(change.retired))]):
            terminal.note("study", f"{old.text} progressed (back after {gap} session{'s' if gap != 1 else ''})"
                          + (f" -> new target {new.text}" if new is not None else ""))

    async def _start_brain(self) -> int:
        cfg = self.cfg
        self._make_plan()
        self.rendered = rendered = prompt.build(self.profile_text, persona=cfg.TUTOR_PERSONA,
                                                memory=self.memory_text, study_plan=self.plan_text)
        profile_api.debug_snapshot(self.student, self.profile_text, cfg.path("LOG_DIR"),
                                   dt.date.today().isoformat())
        sections = " · ".join(f"{k} {v}" for k, v in rendered.sections.items())
        print(f"{DIM}prompt {rendered.tokens} tokens ({sections})"
              f"{' · TRUNCATED: ' + ', '.join(rendered.truncated) if rendered.truncated else ''}{RESET}")

        # --- brain (ADR-027: the REPL talks to the interface, not to Claude) --------
        self._tools = tools = mcp_config.allowed_tools(cfg)
        self._mcp_json = mcp_json = mcp_config.write(cfg) if tools else None
        # Each tier is the NEWEST model the CLI accepts, not whatever its bare alias maps to: the
        # `sonnet` alias ran claude-sonnet-4-6 while claude-sonnet-5 works (2026-09-10, model_tiers.py).
        self._tiers = tiers = model_tiers.Resolver.from_config(cfg)
        tutor_model = await asyncio.to_thread(tiers.resolve, str(cfg.CLAUDE_MODEL))
        self.brain = brain = brain_api.create(
            cfg, registry=registry, model=tutor_model,
            mcp_config=mcp_json,
            mcp_ready_markers=mcp_config.markers(cfg) if mcp_json else None,
            system_prompt=rendered.text,
            allowed_tools=tools,
        )
        if self.mem is not None:
            self.mem.session_id = brain.session_id     # the turn log is named after the session it records
        try:
            await brain.start()
        except RuntimeError as exc:
            print(f"\n{BOLD}cannot start the brain:{RESET} {exc}\n", file=sys.stderr)
            return 2
        return 0

    async def _model_for(self, fresh: config.Config) -> str:
        return await asyncio.to_thread(self._tiers.resolve, str(fresh.CLAUDE_MODEL))

    # ------------------------------------------------------------------ the lesson
    async def account(self, b) -> None:
        # The status bar (user request 2026-09-10): how full her context is, this month's use and the
        # rate-limit window — read after every turn from the brain's own last `result`.
        m = dict(getattr(b, "meters", {}) or {})
        month = await asyncio.to_thread(self.ledger.add_turn)
        rl = dict(getattr(b, "rate_limit", {}) or {})
        if self.hub is not None:
            await self.hub.meters(context_tokens=m.get("context_tokens"), context_window=m.get("context_window"),
                                  month_turns=month["turns"],
                                  limit_status=rl.get("status"), limit_type=rl.get("rateLimitType"),
                                  limit_resets_at=rl.get("resetsAt"))

    async def open_lesson(self) -> None:
        """Sensei speaks first (spec §5c): she finds a subject and opens on it, rather than waiting
        for the student to produce one. This is the turn that pays for the search. Then the older
        sessions are summarised in the background."""
        if not self.args.no_open:
            await one_turn(self.brain, OPENING_NUDGE, self.voice, mem=self.mem, opening=True, noted=self.note_turn)
            await self.account(self.brain)
        # Older sessions are summarised now, in the background: the student is already in the lesson,
        # and a summary that fails here simply waits for the next launch (2026-09-12).
        if self.mem is not None and self.mem.pending_logs():
            self.catchup = asyncio.create_task(summarise(self.cfg, self.mem))
            self.catchup.add_done_callback(lambda t: None if t.cancelled() else t.exception())

    async def switch_persona(self, name: str):
        """Live tutor change (settings panel): a fresh session with the new persona, speaking in
        the voice that persona declares (ADR-030). Returns the started brain. The old one keeps
        answering until this one is up, so a failed switch leaves a tutor rather than nobody."""
        fresh = config.load()
        mem = self.mem
        # The new tutor reads THEIR memory of this student, not the one the last tutor had — and
        # the same targets: the plan is the student's, whoever is teaching (spec §6c).
        text = prompt.build(self.profile_text, persona=name,
                            memory=mem.for_persona(name).render() if mem is not None else "",
                            study_plan=self.plan_text).text
        new = brain_api.create(fresh, registry=registry, mcp_config=self._mcp_json,
                               model=await self._model_for(fresh),
                               mcp_ready_markers=mcp_config.markers(fresh) if self._mcp_json else None,
                               system_prompt=text, allowed_tools=self._tools)
        await new.start()
        if self.voice is not None:
            tts_new = await VoicevoxClient.from_config_async(fresh)
            try:
                await asyncio.to_thread(tts_new.warm_up)
            except VoicevoxError:
                pass                  # the first sentence pays for loading the style instead
            self.voice.tts = tts_new
        old, self.brain = self.brain, new
        if mem is not None:
            # A different tutor is a different memory (user, 2026-09-12): their own last lesson,
            # their own life; the student's own facts follow them across. Changed IN PLACE,
            # because the turn recorder and the rotation handoff already hold this object.
            mem.switch_to(name)
            mem.session_id = new.session_id
        await old.aclose()
        return new

    async def spawn_rotation(self, handoff: str):
        """A replacement session for rotation (ADR-032): the same tutor, newest model for the tier
        and tools, plus the lesson so far. Started here, swapped in at a turn boundary by listen()."""
        fresh = config.load()
        text = prompt.build(self.profile_text, persona=str(fresh.TUTOR_PERSONA), memory=self.memory_text,
                            handoff=handoff, study_plan=self.plan_text).text
        new = brain_api.create(fresh, registry=registry, mcp_config=self._mcp_json,
                               mcp_ready_markers=mcp_config.markers(fresh) if self._mcp_json else None,
                               system_prompt=text, allowed_tools=self._tools,
                               model=await self._model_for(fresh))
        try:
            await new.start()
        except BaseException:
            # Cancelled (rotator.discard during a persona switch or a resync) or failed after the
            # process exists: it is ours until it is started, so it is ours to close (session.py).
            await new.aclose()
            raise
        return new

    async def resync(self) -> None:
        """Manual Refresh from the page (spec §5b) — with launch, the ONLY time the SRS APIs are
        called (ADR-024). Read-only as ever; tokens are re-read so a token saved in the settings
        panel applies. The new profile reaches her through a fresh session (listen)."""
        if self.args.no_srs:
            raise RuntimeError("study data is off for this session (--no-srs)")
        wait = RESYNC_MIN_S - (time.monotonic() - self.last_sync)
        if wait > 0:
            raise RuntimeError(f"refreshed a moment ago - try again in {wait:.0f} s")
        self.last_sync = time.monotonic()
        fresh = config.load()
        student = await asyncio.to_thread(
            profile_api.build, fresh.WANIKANI_TOKEN, fresh.BUNPRO_API_TOKEN, fresh.path("CACHE_DIR") / "srs",
            fresh.SRS_CACHE_TTL_S, fresh.SRS_FETCH_BUDGET_S, registry, force=True)
        self.student = student
        self.profile_text = profile_api.render(student)
        self.annotator.known = set(student.wanikani.known_kanji) if student.wanikani else set()
        self.annotator.kanji_readings = student.wanikani.kanji_readings if student.wanikani else {}
        if self.hub is not None:              # their words moved on: so does the blue in the chat
            self.hub.study = study_api.Study.from_profile(student, getattr(self.annotator, 'tokens', None))
        if self.plan is not None:             # and so do today's targets, keeping this session's progress
            self.plan = self.plan.reselect(study_api.Study.from_profile(student, getattr(self.annotator, 'tokens', None)).items)
            self.plan_text = study_plan.render(self.plan)

    def adopt(self, new) -> None:
        # After a rotation the replacement is THE session: the one closed at the end, and the one
        # a tutor switch replaces.
        self.brain = new

    # ------------------------------------------------------------------ the exit
    async def close(self) -> None:
        """Everything down, in the order that never leaves a process behind. Safe to call after
        a partial launch: what was never opened is skipped."""
        if self.catchup is not None:
            self.catchup.cancel()
        if self.summary is not None and not self.summary.done():
            self.summary.cancel()      # it outran the launch's patience; do not outlive the lesson
        await self.explainer.aclose()
        if self.voice is not None:
            await self.voice.aclose()
        if self.brain is not None:
            await self.brain.aclose()
        if self.server_task is not None:
            from backend import app as web
            await web.shutdown(self.server_task)
        await stop_containers(self.hub)


# ---------------------------------------------------------------------- the parts
async def load_stt(cfg):
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


async def check_microphone(cfg) -> bool:
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
        # One line, not a checklist: it printed at every launch, and the cause is almost
        # always the switch on the headset (user, 2026-09-12).
        print(f"{BOLD}microphone ({device}) is silent{RESET} - rms {level:.6f}, which is a mute, "
              f"not a quiet room. Check the headset switch; the meter shows when you are heard.")
    else:
        print(f"{DIM}microphone ready: {device} · rms {level:.4f}{RESET}")
    return True


async def stop_containers(hub) -> None:
    """Take the containers down, but only if the page's stop button asked for it.

    The page's button means "I am done for today" (user, 2026-09-12), so it does what the stop
    script does — VOICEVOX and SearXNG go too. Ctrl+C deliberately does NOT: that is the "back in a
    minute" exit, and the containers are slow to start and cheap to keep (backend/tools/up.py).
    """
    if hub is None or not getattr(hub, "quit_requested", False):
        return
    from backend.tools.down import main as compose_down

    print(f"{DIM}stopping the containers, as the page asked…{RESET}", flush=True)
    await asyncio.to_thread(compose_down, [])


async def summarise(cfg, mem, limit: int | None = None) -> int:
    """One short-lived, cheap brain that summarises past sessions, then goes away.

    Same Brain interface as the tutor (ADR-027) but its own process, its own model and no tools:
    it must not share the tutor's session, or the summary request would sit in her transcript.
    Every failure path returns 0 — no summary means no recall of that session, not no lesson.
    """
    instructions = (prompt.PROMPTS_DIR / "summarise.md").read_text(encoding="utf-8")
    summary_model = await asyncio.to_thread(model_tiers.Resolver.from_config(cfg).resolve,
                                            str(cfg.MEMORY_SUMMARY_MODEL))
    worker = brain_api.create(cfg, registry=None, allowed_tools=(),
                              model=summary_model,
                              system_prompt=(prompt.PROMPTS_DIR / "summariser.md").read_text(encoding="utf-8"))
    try:
        await asyncio.wait_for(worker.start(), 90)
    except Exception as exc:  # noqa: BLE001 - memory is best-effort by contract
        print(f"{DIM}memory: summariser did not start ({type(exc).__name__}); skipping{RESET}")
        return 0

    async def ask(text: str) -> str:
        # Raises on a failed turn (an API error, a timeout), so summarise_pending leaves that
        # lesson pending for the next launch instead of marking it done on an empty reply.
        return await brain_api.reply_text(worker, text)

    def progress(log, at: int, total: int) -> None:
        if total > 1:                      # the launch summarises one; the background says where it is
            print(chr(13) + DIM + f"memory: summarising {log.stem[:10]} ({at}/{total})…" + RESET, flush=True)

    def footer(log) -> str:
        # Spec §6c: what the lesson's own marks say was practised and progressed, for the topics row.
        return study_plan.summary_footer(log, int(cfg.STUDY_PROGRESS_AFTER))

    try:
        return await asyncio.wait_for(mem.summarise_pending(ask, instructions, limit=limit, on_log=progress,
                                                            footer=footer), 600)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return 0
    finally:
        await worker.aclose()


async def one_turn(brain, text: str, voice=None, mem=None, opening: bool = False,
                   coach: Callable[[str], str] | None = None,
                   noted: Callable[[str, list], None] | None = None) -> None:
    """Send one text turn; print each sentence the moment it closes, with its latency.

    `coach` rewrites what the brain is asked (the study plan's note, spec §6c) — the log records
    `text` as typed; `noted(student, sentences)` is told the turn afterwards, like memory is."""
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
            spoken.append(chunk.as_log())
            print(f"  {DIM}{now:5.2f}s{RESET} {terminal.emotion_tag(chunk.emotion)}{chunk.text}")
            if voice is not None:
                await voice.say(chunk)

    asked = coach(text) if coach is not None and not opening else text
    async for event in brain.turn(asked):
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
        elif isinstance(event, Compacting):
            print(f"  {DIM}· {terminal.compaction_text(event)}{RESET}")
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
            if noted is not None:
                noted("" if opening else text, spoken)
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


async def watch_vram(cfg, gpu: dict, page: page_control.PageControl | None) -> None:
    # Spec §10b / ROADMAP 11: sampled on the status heartbeat, off the conversation path
    # (nvidia-smi runs in a thread), with a warning above the cap — said once per crossing.
    warned = False
    while True:
        reading = await asyncio.to_thread(vram_mod.read)
        if reading is not None:
            gpu["mib"] = reading.used_mib
            too_much = vram_mod.over(reading, float(cfg.VRAM_WARN_GB))
            if too_much and not warned:
                terminal.note("gpu", f"{reading.used_gb:.1f} GB in use - over the "
                              f"{cfg.VRAM_WARN_GB} GB cap (spec §10b)", bold=True)
            warned = too_much
            if page is not None:
                await page.status("gpu", "over" if too_much else "ok",
                                  f"{reading.used_gb:.1f} of {reading.total_gb:.0f} GB")
                await page.hub.meters(vram_used_mib=reading.used_mib, vram_total_mib=reading.total_mib)
        await asyncio.sleep(float(cfg.STATUS_HEARTBEAT_S))


# ---------------------------------------------------------------------- the voice conversation
async def listen(lesson: Lesson) -> None:
    """Full voice loop: speak to him, he answers aloud (spec §2).

    Everything is already loaded by the time this runs — see `Lesson.build()`.
    """
    from backend.vad import VoiceActivityDetector
    from backend.voice_loop import VoiceLoop

    cfg, hub, mem, voice, stt = lesson.cfg, lesson.hub, lesson.mem, lesson.voice, lesson.stt
    if voice is None or stt is None:
        # --listen implies --speak, so getting here means the mouth failed to open, not that the
        # user forgot a flag. Say which, or they go hunting through argv for a problem that is in
        # Docker.
        print(f"{BOLD}cannot listen without a voice{RESET} — VOICEVOX is not answering at "
              f"{cfg.VOICEVOX_URL}. Start it with `docker compose up -d voicevox` and try again.",
              file=sys.stderr)
        return

    vad = await VoiceActivityDetector.from_config_async(cfg)
    aloop = asyncio.get_running_loop()
    #: The latest GPU memory sample (spec §10b), for the turn log. Filled by the heartbeat below.
    gpu: dict = {"mib": None}
    meter = terminal.LevelMeter()
    sender = page_control.LevelSender(hub) if hub is not None else None
    page: page_control.PageControl | None = None      # built once the loop and rotator exist

    def show_level(level: float, prob: float) -> None:
        ptt = (("open" if loop._ptt_open else "closed") if loop.ptt else None)
        peak = meter.update(level, prob, hot=prob >= vad.active_threshold, ptt=ptt)
        if sender is not None:
            sender.offer(peak, prob)          # the page's meter: the newest value, sent at 10 Hz

    def interrupted() -> None:
        # Barge-in (spec §8): say so here, and tell the page to stop NOW and drop the rest of the
        # turn (page_control). Thread-safe: the terminal's push-to-talk key reports from its own thread.
        print(f"\r{BOLD}— interrupted —{RESET}")
        if page is not None:
            page.bargein_threadsafe(aloop)

    def notice(text: str) -> None:
        # The loop could not do what was asked (a dropped utterance and why): both screens.
        terminal.note("voice", text, bold=True)
        if page is not None:
            page.notice(text)

    loop = VoiceLoop(
        turn_mode=cfg.TURN_MODE,
        brain=lesson.brain, stt=stt, vad=vad, voice=voice, input_device=cfg.AUDIO_INPUT_DEVICE,
        on_level=show_level,
        on_state=lambda s: print(f"\r{DIM}[{s}]{RESET}" + " " * 50, end="", flush=True) if s != "listening" else None,
        on_transcript=lambda t: print(
            f"\r{BOLD}you:{RESET} {t.text}" if t
            else f"\r{DIM}(discarded: {t.reason} — {t.text[:40]}){RESET}"),
        on_chunk=lambda c, ms: print(f"  {DIM}{ms / 1000:5.2f}s{RESET} {terminal.emotion_tag(c.emotion)}{c.text}"),
        on_bargein=interrupted,
        on_notice=notice,
        quiet_over_floor=float(cfg.QUIET_OVER_FLOOR),
        handover_timeout_s=float(cfg.PTT_HANDOVER_TIMEOUT_S),
    )

    # Pre-emptive rotation (ADR-032): off unless CONTEXT_ROTATE_AT > 0.
    launched_at = memory_api.launch_stamp()

    def lesson_so_far() -> str:
        # The handoff: the lesson's own turn log, newest kept — deterministic, no model call.
        # Only turns since THIS launch: with nothing spoken yet it is "", no heading is added, and
        # a replacement spawned before the first turn (Refresh, a tutor switch) opens normally.
        if mem is not None:
            return memory_api.excerpt_of(mem.log_path(), HANDOFF_CHARS, since=launched_at)
        lines = []
        for t in loop.timings[-12:]:
            if t.transcript:
                lines.append(f"STUDENT: {t.transcript}")
            said = "".join(str(s.get("text", "")) for s in t.sentences)
            if said:
                lines.append(f"TUTOR: {said}")
        return "\n".join(lines)[-HANDOFF_CHARS:]

    learned_path = cfg.path("CACHE_DIR") / session_api.LEARNED_FILE
    rotator = session_api.Rotator(
        session_api.effective_threshold(cfg.CONTEXT_ROTATE_AT, session_api.load_learned(learned_path)),
        lesson.spawn_rotation, lesson_so_far,
        log=lambda s: terminal.note("rotation", s))

    def report_turn(t) -> None:
        # Spec §10 / ROADMAP 10: every turn's breakdown with the Claude stage taken apart, the
        # session's rolling p50/p90, a warning over LATENCY_WARN_S, and the same record to the
        # page as `timing`. Barged-in turns are recorded but kept out of the percentiles.
        done = [x.voice_to_voice_ms() for x in loop.timings if not x.barged_in and x.voice_to_voice_ms()]
        t.session_p50_ms, t.session_p90_ms = percentile(done, 50), percentile(done, 90)
        print(terminal.timing_line(t, done, float(cfg.LATENCY_WARN_S)) + "\n")
        if page is not None:
            page.timing(t, len(done))
        aloop.create_task(lesson.account(loop.brain))
        # ADR-032, after the turn: close the session a rotation replaced (it has now spoken), then
        # arm on this turn's context and start a replacement in the background if needed.
        aloop.create_task(rotator.settle())
        rotator.observe(loop.brain)
        rotator.prepare()

    def remember(t) -> None:
        # Record in the speaking gap: on_turn fires from _turn's finally, after TurnComplete, so
        # nothing here can sit between the student stopping and her first audio (spec §6b).
        # Record FIRST: `report_turn` arms and prepares the rotation, whose handoff is read from
        # this very log — prepared before the write, it missed the turn that just ended.
        if mem is None:
            lesson.note_turn(t.transcript, list(t.sentences))     # the plan still counts (spec §6c)
            report_turn(t)
            return
        last = getattr(loop.brain, "last_turn", None) or {}     # spec §6b: tools[], usage
        mem.record_turn(student=t.transcript, tutor_sentences=list(t.sentences),
                        tools=sanitised(list(last.get("tools") or [])),   # spec §11: once, here
                        usage=dict(last.get("usage") or {}),
                        student_extra={"stt_ms": round(t.stt_ms)},
                        latency={"first_audio_ms": round(t.first_audio_ms),
                                 "first_play_ms": round(t.first_play_ms),   # playback start (2026-09-12)
                                 "voice_to_voice_ms": round(t.voice_to_voice_ms()),
                                 "ttft_ms": round(t.ttft_ms) if t.ttft_ms else None,
                                 "thinking_chars": t.thinking_chars,
                                 "barged_in": t.barged_in,
                                 # rolling over the session so far (spec §10)
                                 "session_p50_ms": round(t.session_p50_ms) if t.session_p50_ms else None,
                                 "session_p90_ms": round(t.session_p90_ms) if t.session_p90_ms else None,
                                 "vram_mib": gpu["mib"]})           # spec §10b, latest sample
        lesson.note_turn(t.transcript, list(t.sentences))         # the raw transcript, as logged
        report_turn(t)

    loop.on_turn = remember
    # The coach note rides above the student's words to the brain only (spec §6c): the transcript
    # the page shows, the log records and the timing names is the raw one.
    loop.coach = lesson.coach

    def compacting(ev: Compacting) -> None:
        # Claude condensing on its own schedule (spec §6b): explain the silence on both screens,
        # and if it came before our rotation did, rotate earlier from now on (ADR-032 amendment).
        text = terminal.compaction_text(ev)
        terminal.note("memory", text, bold=bool(ev.error))
        if page is not None:
            state = "running" if ev.active else ("failed" if ev.error else "done")
            page.status_soon("compaction", state, text, remember=False)
        window = (getattr(loop.brain, "meters", None) or {}).get("context_window")
        lowered = rotator.learn(ev.trigger, ev.pre_tokens, window)
        if lowered is not None:
            session_api.save_learned(learned_path, lowered, pre_tokens=ev.pre_tokens, window=window,
                                     at=time.strftime("%Y-%m-%dT%H:%M:%S"))

    loop.on_compacting = compacting

    def swap_if_ready() -> None:
        new = rotator.take(loop.brain)
        if new is not None:
            loop.brain = new
            lesson.adopt(new)

    loop.before_turn = swap_if_ready

    def device_changed(state: str, detail: str) -> None:
        # Unplugged, missing at launch, back again, or on the default instead of the chosen one:
        # say so on both screens, or a dead mic is indistinguishable from a quiet student.
        terminal.note("microphone " + state, detail, bold=state in ("missing", "lost"), pad=10)
        if page is not None:
            page.status_soon("microphone", state, detail)

    loop.on_device = device_changed

    from backend import settings_view
    from backend.device_watch import DeviceWatch

    def devices_changed(options: dict) -> None:
        # Plugged in or pulled out (spec §9): refresh the panel's pickers, and if we are on the
        # default only because the chosen mic was missing, go and get it.
        settings_view.latest = options
        loop.reopen_mic()
        if hub is not None:
            aloop.create_task(hub.push_settings())

    watch = DeviceWatch(devices_changed)
    await watch.start()

    if hub is not None:
        page = page_control.PageControl(hub, loop, rotator=rotator, resync=lesson.resync,
                                        switch_persona=lesson.switch_persona)
        page.attach()
        sender.start()
    vram_task = aloop.create_task(watch_vram(cfg, gpu, page))

    device = cfg.AUDIO_INPUT_DEVICE or "system default"
    keys = terminal.ptt_keys(loop, aloop) if loop.ptt else None
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
        if sender is not None:
            sender.stop()
        await rotator.aclose()
        if (line := terminal.session_report(loop.timings)) is not None:
            print(line)
