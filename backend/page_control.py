"""The browser's side of a lesson: what the page's buttons do, and what the page is told.

`PageControl` is the dispatch for `control` messages (quit / resync / new_topic / start / cancel /
stop), the tutor switch and Refresh started from the settings panel, and every status line the
orchestrator sends the page. `LevelSender` feeds the page's microphone meter. Split out of
repl.py on 2026-09-12; the dated findings moved with the code.

Every string that reaches `hub.status()` passes through the registry's sanitiser HERE, once, at
the boundary (spec §11): an exception's text can quote a header, and the page must never see one.
"""
from __future__ import annotations

import asyncio
import traceback
from typing import Any, Awaitable, Callable

from backend import terminal
from backend.status import registry
from backend.terminal import BOLD, CR, DIM, RESET

#: The page's New topic button. The same words tutor.md tells her to treat as 「話題を変えて」.
TOPIC_NUDGE = "（話題を変えて）"
#: After a live tutor change: the new person introduces themselves and carries on the lesson.
TUTOR_NUDGE = "（先生が交代しました。新しい先生として自己紹介して、レッスンを続けてください。）"
#: A tutor switch that has not brought up the new session by then has failed: say so, keep the old
#: tutor. Starting one normally takes a couple of seconds (measured 1.2 s, 2026-09-11).
SWITCH_TIMEOUT_S = 90.0


class LevelSender:
    """The page's meter at 10 Hz from a per-frame source: the newest value wins.

    One long-lived task, not a task per frame — the old meter spawned ~10 tasks a second, each
    awaiting the socket, and a slow page could pile them up (2026-09-12). `offer()` overwrites;
    the pump sends whatever is newest every `every_s` and nothing when nothing changed.
    """

    def __init__(self, hub, every_s: float = 0.1) -> None:
        self.hub = hub
        self.every_s = every_s
        self.latest: tuple[float, float] | None = None
        self.sent = 0
        self._task: asyncio.Task | None = None

    def offer(self, level: float, prob: float) -> None:
        self.latest = (level, prob)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._pump())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _pump(self) -> None:
        while True:
            await asyncio.sleep(self.every_s)
            latest, self.latest = self.latest, None
            if latest is not None:
                self.sent += 1
                await self.hub.level(*latest)


class PageControl:
    """One page (a `Hub`) driving one voice loop.

    `loop` is the VoiceLoop; `rotator` the session rotator (ADR-032); `resync` and
    `switch_persona` come from the orchestrator (None when that mode has none). `sanitize` is
    the registry's, injected so a test can prove the routing without the global registry.
    """

    def __init__(self, hub, loop, *, rotator=None,
                 resync: Callable[[], Awaitable[None]] | None = None,
                 switch_persona: Callable[[str], Awaitable[Any]] | None = None,
                 sanitize: Callable[[str], str] | None = None) -> None:
        self.hub = hub
        self.loop = loop
        self.rotator = rotator
        self.resync = resync
        self.switch_persona = switch_persona
        self.sanitize = sanitize or registry.sanitize
        #: Background jobs started from the page. The event loop holds tasks only weakly, so one
        #: nobody references can vanish mid-flight; and one that dies must say so rather than
        #: disappear.
        self.background: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ status plumbing
    async def status(self, service: str, state: str, detail: str = "", *, remember: bool = True) -> None:
        """Every status line to the page goes through here: sanitised once (spec §11)."""
        await self.hub.status(service, state, self.sanitize(detail), remember=remember)

    def tell(self, coro) -> None:
        """Fire-and-forget to the page, from the event loop."""
        asyncio.get_running_loop().create_task(coro)

    def status_soon(self, service: str, state: str, detail: str = "", *, remember: bool = True) -> None:
        self.tell(self.status(service, state, detail, remember=remember))

    def ack(self, state: str, detail: str) -> None:
        # Every press gets an answer the page can show. A press that is never answered
        # is how the page recognises a dead link, and it then reconnects (2026-09-10:
        # presses were vanishing and the page could not tell anyone).
        self.status_soon("ptt", state, detail, remember=False)

    def notice(self, text: str) -> None:
        """The loop could not do what was asked (a dropped utterance and why): the page sees it
        as an event, not a state."""
        self.status_soon("voice", "busy", text, remember=False)

    def bargein_threadsafe(self, aloop: asyncio.AbstractEventLoop) -> None:
        # Barge-in (spec §8): tell the page to stop NOW and drop the rest of the turn. Without it
        # the page played on to the end of the sentence it had (M3b, 2026-09-11).
        # Thread-safe: the terminal's push-to-talk key reports from its own thread.
        aloop.call_soon_threadsafe(lambda: aloop.create_task(self.hub.bargein()))

    def timing(self, t, turns: int) -> None:
        """The turn's record to the page as `timing` (spec §10): `first_play_ms` beside
        `first_audio_ms` — playback start and synthesis done (2026-09-12)."""
        self.tell(self.hub.timing(
            stt_ms=t.stt_ms, first_chunk_ms=t.first_chunk_ms, first_audio_ms=t.first_audio_ms,
            first_play_ms=t.first_play_ms,
            total_ms=t.total_ms, barged_in=t.barged_in, ttft_ms=t.ttft_ms,
            thinking_chars=t.thinking_chars, p50_ms=t.session_p50_ms, p90_ms=t.session_p90_ms,
            turns=turns))

    def in_background(self, coro, what: str) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.background.add(task)

        def finished(t: asyncio.Task) -> None:
            self.background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                exc = t.exception()
                terminal.note(what, f"failed: {type(exc).__name__}: {exc}", bold=True)

        task.add_done_callback(finished)

    # ------------------------------------------------------------------ wiring
    def attach(self) -> None:
        """Take the page's buttons, the settings panel, and the loop's state and transcript."""
        hub, loop = self.hub, self.loop
        # The browser is a better push-to-talk button than the terminal, because a browser can
        # see key RELEASE. `control: start`/`stop` are exactly the ptt edges (spec §8/§9), so
        # holding the key there gives real hold-to-talk instead of the toggle a TTY is limited to.
        # The terminal binding stays live as well — either can drive the same turn.
        hub.on_control = self.from_browser
        hub.on_settings = self.settings_saved

        # The browser was getting audio and nothing else: no transcript, no state. Silence after
        # a press then looked the same as a broken microphone, when it might equally be a
        # discarded transcript or the brain still thinking. These are cheap and they are the
        # difference between "it is not working" and "it did not hear me".
        def on_state(s: str) -> None:
            if s != "listening":
                terminal.say(CR + DIM + "[" + s + "]" + RESET + " " * 50, end="")
            self.tell(hub.state(s))

        def on_transcript(t) -> None:
            terminal.say(CR + BOLD + "you:" + RESET + " " + t.text if t
                         else CR + DIM + "(discarded: " + t.reason + ")" + RESET)
            self.tell(hub.transcript(t.text, bool(t), getattr(t, "reason", "")))

        loop.on_state = on_state
        loop.on_transcript = on_transcript

    def settings_saved(self, keys: list[str]) -> None:
        # settings_view.LIVE: devices and the tutor take effect now; the rest at next launch.
        from backend import config
        fresh = config.load()
        if "AUDIO_INPUT_DEVICE" in keys:
            self.loop.reopen_mic(fresh.AUDIO_INPUT_DEVICE, changed=True)
        if "AUDIO_OUTPUT_DEVICE" in keys:
            self.loop.voice.set_device(fresh.AUDIO_OUTPUT_DEVICE)
        if "TUTOR_PERSONA" in keys and self.switch_persona is not None:
            self.in_background(self.change_tutor(str(fresh.TUTOR_PERSONA)), "tutor")

    # ------------------------------------------------------------------ the panel's jobs
    async def do_resync(self) -> None:
        # The page's Refresh button (spec §5b): fetch, then hand the new profile to a fresh
        # session that takes over at her next answer — the lesson carries on (ADR-032 handoff).
        await self.status("resync", "syncing", "fetching your WaniKani and Bunpro progress...", remember=False)
        try:
            if self.resync is None:
                raise RuntimeError("refresh is not available in this mode")
            await self.resync()
        except Exception as exc:  # noqa: BLE001 - say why on the page; the lesson is unaffected
            await self.status("resync", "failed", str(exc)[:200], remember=False)
            return
        if self.rotator is not None:
            await self.rotator.rotate_next_turn("study data refreshed")
        await self.status("resync", "done", "refreshed - she has it from her next answer", remember=False)

    async def change_tutor(self, name: str) -> None:
        # A different person is about to speak: stop the current one, bring up the new persona
        # and voice, then let them introduce themselves. Used to wait for the next launch, so the
        # panel's tutor cards seemed to do nothing (2026-09-10).
        # Everything inside the try, with a deadline and the full traceback: on 2026-09-11 a switch
        # died between "saved" and "started" and nothing said why — the old tutor simply went on.
        loop = self.loop
        try:
            await self.status("tutor", "switching", f"switching to {name}...", remember=False)
            terminal.note("tutor", "switching to " + name, bold=True, pad=20)
            if loop._turn_task is not None and not loop._turn_task.done():
                loop.voice.cancel()
                loop._turn_task.cancel()
            loop.brain = await asyncio.wait_for(self.switch_persona(name), SWITCH_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - the old tutor carries on rather than nobody
            traceback.print_exc()
            reason = "timed out" if isinstance(exc, TimeoutError) else f"{type(exc).__name__}: {exc}"
            terminal.note("tutor", "could not switch to " + name + ": " + reason, bold=True)
            await self.status("tutor", "failed", f"could not switch to {name} - {reason}"[:200],
                              remember=False)
            return
        if self.rotator is not None:
            await self.rotator.discard()     # a replacement built for the previous tutor is no use now
        await self.status("tutor", "ok", f"now teaching: {name}", remember=False)
        loop.ask(TUTOR_NUDGE)

    # ------------------------------------------------------------------ the buttons
    def from_browser(self, action: str) -> None:
        # Report what the press actually produced. "recording" in the browser only proves the
        # message arrived; whether the microphone thread is feeding the buffer, and whether
        # the result was long enough to become a turn, are separate questions that look
        # identical from the UI (2026-09-10).
        # Visible on the console: a press that never arrives and a press that arrives but
        # produces no audio are different problems, and they look identical otherwise.
        loop = self.loop
        terminal.say(CR + DIM + "[browser: " + action + "]" + RESET + " " * 30, end="")
        if action == "quit":
            # The page's stop button means "I am done", not "pause the tutor" (user,
            # 2026-09-12): stopping the loop ends the listen, run()'s close() closes the claude
            # subprocess, the speech queue and this server, and then the containers go down
            # too - exactly what `.\stop` does. Ctrl+C still leaves them up, because that is
            # the "carry on in a minute" exit.
            terminal.say(CR + BOLD + "stop requested from the page - shutting everything down" + RESET)
            self.hub.quit_requested = True
            loop.stop()
            return
        if action == "resync":
            self.tell(self.do_resync())
            return
        if action == "new_topic":
            loop.ask(TOPIC_NUDGE)
            self.ack("topic", "finding a new topic...")
            return
        if action == "start":
            if not loop.ptt:
                self.ack("off", "hands-free mode is on - just speak")
                return
            loop.ptt_begin()
            self.ack("recording", "listening - release to send, ALT GR to cancel")
        elif action == "cancel":
            # The student started a sentence and wants it gone. Dropping it here means they can
            # let the talk key go without the microphone's last seconds becoming a turn.
            if loop.ptt_cancel():
                self.ack("cancelled", "dropped - nothing was sent, press SPACE to start again")
        elif action == "stop":
            self._stop_pressed()

    def _stop_pressed(self) -> None:
        import numpy as np
        from backend import audio as audio_mod
        from backend import voice_loop as voice_loop_mod

        loop = self.loop
        buf = list(loop._ptt_buf)
        frames = len(buf)
        seconds = frames * voice_loop_mod.vad_frame_samples() / audio_mod.SAMPLE_RATE
        rms = float(np.sqrt(np.mean(np.square(np.concatenate(buf))))) if buf else 0.0
        before = loop._turn_task
        loop.ptt_end()
        started = loop._turn_task is not None and loop._turn_task is not before
        terminal.say(CR + DIM + "[browser: stop] " + str(frames) + " frames ("
                     + format(seconds, ".1f") + "s, rms " + format(rms, ".5f") + ") -> "
                     + ("turn started" if started else "NO TURN")
                     + "  mic_alive=" + str(loop._mic.is_alive() if loop._mic else False)
                     + " frames_seen=" + str(loop.frames_seen)
                     + (" err=" + loop.capture_error if loop.capture_error else "")
                     + RESET + " " * 10)
        if not loop.ptt:
            return
        if frames == 0:
            self.ack("empty", "no audio arrived from the microphone")
        elif rms < audio_mod.SILENT_RMS:
            # Digital silence: the device is open but muted (boom up, mute button, or
            # Windows privacy). Sending it anyway only earns a discarded hallucination.
            self.ack("silent", f"your microphone sent silence ({seconds:.1f}s) - is it muted?")
        elif started:
            self.ack("sent", f"sent {seconds:.1f}s - she is listening to it")
        elif seconds * 1000 < loop.vad.min_speech_ms:
            self.ack("short", "too short to send - hold SPACE while you speak")
        else:
            self.ack("busy", "she is still working on your last turn")
