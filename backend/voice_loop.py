"""The voice loop: mic -> VAD -> Whisper -> brain -> VOICEVOX -> speakers (spec §2, M2).

This is the turn cycle of §2 with no browser in front of it yet. The microphone runs on its own
thread because PortAudio reads are blocking; everything else is one asyncio task, so the VAD keeps
seeing frames *while the avatar is speaking* — which is what makes barge-in possible at all.

Turn-taking policy lives here rather than in the VAD (ADR-006): the detector reports what it
hears, this decides what it means given what the tutor is doing.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from backend import audio as audio_mod
from backend.audio import SAMPLE_RATE
from backend.brain import (BrainError, Compacting, RateLimited, TextDelta, Thinking, ToolCall, ToolOutcome,
                           TurnComplete)
from backend.chunker import SentenceChunker
from backend.speaker import SpeechQueue
from backend.stt import QUIET_RMS, SpeechToText, Transcript
from backend.vad import FRAME_SAMPLES, EventKind, Mode, VoiceActivityDetector


def vad_frame_samples() -> int:
    return FRAME_SAMPLES


#: Audio this many times above the room's own level is not "quiet" (see VoiceLoop.quiet_rms).
#: Default; the live value is QUIET_OVER_FLOOR in config.py (spec §11).
QUIET_OVER_FLOOR = 4.0
#: Default for `handover_timeout_s` (PTT_HANDOVER_TIMEOUT_S in config.py).
PTT_HANDOVER_TIMEOUT_S = 1.0


@dataclass
class TurnTiming:
    """Voice-to-voice, broken down (spec §10). Recorded per turn so p90 is measurable."""

    speech_end_at: float
    stt_ms: float = 0.0
    first_chunk_ms: float = 0.0
    #: The first sentence's audio EXISTS (synthesis done, queued). Not yet heard.
    first_audio_ms: float = 0.0
    #: The first sentence STARTS PLAYING — handed to the local stream, or sent to the page (the
    #: page does not report back, so the WebSocket hop is not in it). This is the honest
    #: voice-to-voice number; `first_audio_ms` was stamped as if it were (2026-09-12). 0.0 when
    #: nothing played (an interrupted or failed turn, or a voice that reports no playback).
    first_play_ms: float = 0.0
    total_ms: float = 0.0
    transcript: str = ""
    chunks: int = 0
    #: What she said back, sentence by sentence, for the turn log (spec §6b). Filled as each
    #: sentence closes, so a barged-in turn still records what was actually spoken.
    sentences: list = field(default_factory=list)
    #: The student talked over this turn, so it was cut short. Recorded, but never counted in the
    #: §10 p90 as a completed turn — it did not fail to be fast, it was interrupted.
    barged_in: bool = False
    #: The brain's own view of the Claude stage (spec §10): time to first token, how much it
    #: thought before speaking, and tool time. Measured 2026-09-10: the stage tracks thinking.
    ttft_ms: float | None = None
    thinking_chars: int = 0
    tool_ms: float | None = None
    #: This session's rolling voice->voice p50/p90 as of this turn (set by the REPL's reporter).
    session_p50_ms: float | None = None
    session_p90_ms: float | None = None
    #: The provider condensed the conversation during this turn (ADR-032's fallback): how long it
    #: took, so the slow turn it causes is logged as what it is (spec §6b), not a mystery.
    compaction_ms: float | None = None

    def voice_to_voice_ms(self) -> float:
        """Speech end -> her voice starts (spec §10): playback start when it was observed, else
        the moment the audio existed (a voice that cannot report playback, or none at all)."""
        return self.first_play_ms or self.first_audio_ms


@dataclass
class VoiceLoop:
    """One live conversation. `run()` until the user stops it."""

    brain: object
    stt: SpeechToText
    vad: VoiceActivityDetector
    voice: SpeechQueue
    input_device: str | int | None = None
    #: "vad" — silence ends the turn. "ptt" — you do, by releasing the key (spec §9).
    #: Under ptt the VAD still runs: it drives the level meter and barge-in detection. It simply
    #: stops deciding when a turn ends, which is the only judgement it gets wrong.
    turn_mode: str = "ptt"
    on_state: Callable[[str], None] | None = None
    on_transcript: Callable[[Transcript], None] | None = None
    on_chunk: Callable[[object, float], None] | None = None
    on_turn: Callable[[TurnTiming], None] | None = None
    on_bargein: Callable[[], None] | None = None
    #: (level, speech probability) per frame — so the user can SEE that they are being heard.
    on_level: Callable[[float, float], None] | None = None
    #: (state, detail) when the microphone changes: ok | fallback | missing | lost (spec §9).
    on_device: Callable[[str, str], None] | None = None
    #: Called at the turn boundary, before the brain is asked anything — the one place a rotated
    #: session may take over (ADR-032: never inside a turn).
    before_turn: Callable[[], None] | None = None
    #: The brain is condensing the conversation (start and end) — the caller explains the silence.
    on_compacting: Callable[[Compacting], None] | None = None
    #: One line for the student when the loop could not do what was asked (a dropped utterance
    #: and why). The page and the terminal show it; nothing else reaches them.
    on_notice: Callable[[str], None] | None = None
    #: What counts as quiet, for THIS microphone: this many times the room's own level
    #: (QUIET_OVER_FLOOR in config.py).
    quiet_over_floor: float = QUIET_OVER_FLOOR
    #: Releasing the talk key while the previous turn is still unwinding: how long to wait for
    #: it before dropping the new utterance and saying so (PTT_HANDOVER_TIMEOUT_S in config.py).
    handover_timeout_s: float = PTT_HANDOVER_TIMEOUT_S

    _frames: asyncio.Queue | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _mic: threading.Thread | None = field(default=None, init=False)
    _speaking: bool = field(default=False, init=False)
    #: The turn runs as its own task so the consume loop below never stops feeding the VAD.
    #: Awaiting it inline blocks frame consumption for the whole turn, which overflows the queue
    #: and — worse — makes barge-in impossible, because the detector sees nothing while the avatar
    #: is speaking (fixed 2026-09-09).
    _turn_task: asyncio.Task | None = field(default=None, init=False)
    #: Frames dropped because the loop fell behind. Should stay 0; a rising count is a real signal.
    dropped_frames: int = field(default=0, init=False)
    frames_seen: int = field(default=0, init=False)
    capture_error: str = field(default="", init=False)
    mic_state: str = field(default="", init=False)
    #: The room's own level between turns, for this microphone (see `quiet_rms`).
    _floor: float | None = field(default=None, init=False)
    #: Set to make the mic thread reopen at the next quiet moment (see `reopen_mic`).
    _wake: threading.Event = field(default_factory=threading.Event, init=False)
    _ptt_open: bool = field(default=False, init=False)
    _ptt_buf: list = field(default_factory=list, init=False)
    timings: list[TurnTiming] = field(default_factory=list, init=False)

    # ------------------------------------------------------------------ public
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._frames = asyncio.Queue(maxsize=200)
        self._stop.clear()
        self._mic = threading.Thread(target=self._capture, args=(loop,), daemon=True)
        self._mic.start()
        self._state("listening")
        beat = asyncio.create_task(self._heartbeat()) if os.environ.get("ATAMA_DEBUG_LOOP") else None
        try:
            while not self._stop.is_set():
                frame = await self._frames.get()
                if frame is None:
                    break
                self.frames_seen += 1
                if self._ptt_open:
                    self._ptt_buf.append(frame)
                events = self.vad.push(frame)
                level = float(np.sqrt(np.mean(np.square(frame))))
                prob = getattr(self.vad, "last_probability", 0.0)
                if not self._ptt_open and not self._speaking and prob < 0.2:
                    # The room between turns: what "quiet" means for THIS microphone.
                    self._floor = level if self._floor is None else 0.98 * self._floor + 0.02 * level
                if self.on_level is not None and not self._speaking:
                    self.on_level(level, prob)
                for event in events:
                    await self._handle(event)
        finally:
            if beat is not None:
                beat.cancel()
            self.stop()

    async def _heartbeat(self) -> None:
        """ATAMA_DEBUG_LOOP=1: say what the loop is doing, even while the tutor is speaking.

        The normal meter is suppressed during `_speaking`, so a loop wedged mid-turn looks
        exactly like a dead microphone. This reports regardless.
        """
        last = -1
        while True:
            await asyncio.sleep(2.0)
            task = self._turn_task
            state = ("none" if task is None else
                     "running" if not task.done() else
                     f"done({'cancelled' if task.cancelled() else 'ok'})")
            moved = self.frames_seen - last
            last = self.frames_seen
            print(f"\n[loop] frames={self.frames_seen} (+{moved}/2s, expect ~62) "
                  f"dropped={self.dropped_frames} qsize={self._frames.qsize() if self._frames else -1} "
                  f"mode={self.vad.mode.name} speaking={self._speaking} turn={state} "
                  f"mic_alive={self._mic.is_alive() if self._mic else False} "
                  f"err={self.capture_error or '-'}", flush=True)

    # ------------------------------------------------------------- push to talk
    @property
    def ptt(self) -> bool:
        return self.turn_mode == "ptt"

    def ptt_begin(self) -> None:
        """Key down. Start collecting audio; interrupt the tutor if he is talking.

        Pressing while he speaks is unambiguous — nobody holds a talk key by accident — so this
        is barge-in without any threshold guesswork, which is the whole reason ptt is steadier
        than vad on speakers.
        """
        if not self.ptt or self._ptt_open:
            return
        if self._speaking:
            self.voice.cancel()
            if self._turn_task is not None and not self._turn_task.done():
                self._turn_task.cancel()
            self._speaking = False
            self.vad.enter(Mode.LISTENING)
            if self.on_bargein is not None:
                self.on_bargein()
        self._ptt_buf = []
        self._ptt_open = True
        self._state("listening")

    def ptt_cancel(self) -> bool:
        """Throw away what is being recorded: the student changed their mind mid-sentence.

        ALT GR on the page (spec §8). Unlike `ptt_end` nothing becomes a turn, and the talk key
        can then be released without sending anything — the next press starts clean. True if there
        was a recording to drop.
        """
        if not self.ptt or not self._ptt_open:
            return False
        self._ptt_open = False
        self._ptt_buf = []
        self._state("listening")
        return True

    def ptt_end(self) -> None:
        """Key up. Everything collected becomes the turn — no silence window, no guessing."""
        if not self.ptt or not self._ptt_open:
            return
        self._ptt_open = False
        frames, self._ptt_buf = self._ptt_buf, []
        if not frames:
            return
        audio = np.concatenate(frames)
        if len(audio) < self.vad.min_speech_ms * SAMPLE_RATE // 1000:
            return                      # a stray tap, not an utterance
        previous = self._turn_task
        if previous is not None and not previous.done():
            # The barge-in in ptt_begin cancelled the last turn, but a cancelled task is not
            # done until it has unwound (a synthesis thread to join, a drain to abandon). This
            # used to drop the utterance silently on that race — the student spoke, the tutor
            # stayed listening, nothing said why (2026-09-12). Now: wait briefly, then proceed,
            # or say what happened.
            self._turn_task = asyncio.get_running_loop().create_task(self._turn_after(previous, audio))
            return
        self._turn_task = asyncio.get_running_loop().create_task(self._turn(audio))

    async def _turn_after(self, previous: asyncio.Task, audio: np.ndarray) -> None:
        """Run `_turn` once the previous turn's task has finished unwinding — bounded."""
        self._state("thinking")
        try:
            await asyncio.wait({previous}, timeout=self.handover_timeout_s)
        except asyncio.CancelledError:
            previous.cancel()           # whoever interrupted us meant the old turn too
            raise
        if not previous.done():
            self._notice(f"the previous answer was still stopping after {self.handover_timeout_s:g} s "
                         "— what you just said was not heard, please press and say it again")
            self._state("listening")
            return
        await self._turn(audio)

    def quiet_rms(self) -> float:
        """What counts as near-silence for THIS microphone, for the STT's hallucination filter.

        A few times the room's own level, never below digital silence, and never stricter than
        the fixed QUIET_RMS it replaces — which assumed a loud microphone and, on a quiet headset
        (speech at rms 0.003), rejected real sentences as silence (2026-09-10).
        """
        ceiling = float(getattr(self.stt, "quiet_rms", QUIET_RMS))
        if self._floor is None:
            return ceiling
        return min(ceiling, max(audio_mod.SILENT_RMS * 2, self._floor * self.quiet_over_floor))

    def ask(self, text: str) -> None:
        """Start a turn from text, not speech (the page's New topic button). If she is talking
        or thinking, that is interrupted first — the student asked for something else."""
        if self._turn_task is not None and not self._turn_task.done():
            self.voice.cancel()
            self._turn_task.cancel()
            if self._speaking and self.on_bargein is not None:
                self.on_bargein()
            self._speaking = False
            self.vad.enter(Mode.LISTENING)
        self._turn_task = asyncio.get_running_loop().create_task(self._turn_text(text))

    def reopen_mic(self, device: str | int | None = None, *, changed: bool = False) -> None:
        """Reopen the microphone at the next quiet moment (never mid push-to-talk).

        `changed`: the settings panel picked a different device — use it now. Otherwise this is
        the device watcher saying the list changed, which only matters while we are on the
        system default because the chosen device was missing: it may be back.
        """
        if changed:
            self.input_device = device
            self._wake.set()
        elif self.mic_state == "fallback":
            self._wake.set()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------- inner
    def _capture(self, loop: asyncio.AbstractEventLoop) -> None:
        """Mic thread: PortAudio reads are blocking, so they never touch the event loop."""
        def status(state: str, detail: str) -> None:
            # Called on this thread; the callback belongs to the event loop.
            self.mic_state = state
            self.capture_error = detail if state in ("missing", "lost") else ""
            if self.on_device is not None:
                try:
                    loop.call_soon_threadsafe(self.on_device, state, detail)
                except RuntimeError:
                    pass

        try:
            # Frames must be exactly Silero's window (512 samples @ 16 kHz); the VAD is stateful
            # and a different size silently degrades its judgement rather than erroring.
            # Resilient: an unplugged or absent mic is waited for, not fatal (spec §9). Probing
            # for a returning device never happens while push-to-talk is held.
            for frame in audio_mod.capture_resilient(
                    lambda: self.input_device, frame_samples=vad_frame_samples(), stop=self._stop,
                    on_status=status, idle=lambda: not self._ptt_open, wake=self._wake):
                if self._stop.is_set():
                    break
                try:
                    loop.call_soon_threadsafe(self._offer, frame)
                except RuntimeError:
                    break         # loop gone: the conversation is over, stop reading the mic
        except audio_mod.AudioUnavailable as exc:
            self.capture_error = f"{type(exc).__name__}: {exc}"
        except BaseException as exc:  # noqa: BLE001
            # PortAudioError is NOT AudioUnavailable. Catching only the latter meant any other
            # failure killed this thread with a traceback nobody sees, and the loop then sat
            # forever waiting on frames that would never come — indistinguishable from a mic
            # that is simply quiet (2026-09-10).
            self.capture_error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                loop.call_soon_threadsafe(self._frames.put_nowait, None)  # type: ignore[union-attr]
            except RuntimeError:
                pass

    def _offer(self, frame) -> None:
        """Hand one mic frame to the loop. Runs ON the event loop, and never raises.

        `call_soon_threadsafe` only *schedules* the call, so a `put_nowait` that overflows raises
        here — inside a bare asyncio handle, where nothing catches it and it prints a traceback per
        frame (50 a second). A full queue means we fell behind; the oldest frame is the one worth
        losing, because the VAD only cares about recent audio.
        """
        queue = self._frames
        if queue is None:
            return
        while queue.full():
            try:
                queue.get_nowait()
                self.dropped_frames += 1
            except asyncio.QueueEmpty:
                break
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:            # cannot happen after the drain above; never raise
            self.dropped_frames += 1

    async def _handle(self, event) -> None:
        if event.kind is EventKind.SPEECH_START and self._speaking and not self.ptt:
            # The student is talking over the avatar: stop, drop the rest, take the new turn.
            self.voice.cancel()
            if self._turn_task is not None and not self._turn_task.done():
                self._turn_task.cancel()     # _turn's finally records the timing and marks it
            # Reset here as well as in _turn's finally, and deliberately: the student is talking
            # *now*, so the detector needs LISTENING thresholds now, not whenever the cancellation
            # finishes unwinding. Both paths are idempotent, and a new turn cannot start until the
            # cancelled task reports done, so the late finally cannot clobber a fresh turn.
            self._speaking = False
            self.vad.enter(Mode.LISTENING)
            self._state("listening")
            if self.on_bargein is not None:
                self.on_bargein()
        elif event.kind is EventKind.SPEECH_END and event.audio is not None:
            if self.ptt:
                return               # the key ends turns here, not the silence window
            if self._turn_task is not None and not self._turn_task.done():
                return                       # a turn is already in flight; one at a time
            self._turn_task = asyncio.create_task(self._turn(event.audio))

    async def _turn(self, audio: np.ndarray) -> None:
        heard_at = time.monotonic()
        self._state("thinking")

        started = time.monotonic()
        transcript = await asyncio.to_thread(self.stt.listen, audio, self.quiet_rms())
        timing = TurnTiming(speech_end_at=heard_at, stt_ms=(time.monotonic() - started) * 1000.0,
                            transcript=transcript.text)
        if self.on_transcript is not None:
            self.on_transcript(transcript)
        if not transcript:
            self._state("listening")
            self.vad.enter(Mode.LISTENING)
            return
        await self._reply(transcript.text, heard_at, timing)

    async def _turn_text(self, text: str) -> None:
        """A turn that starts from text instead of speech — the page's New topic button."""
        heard_at = time.monotonic()
        self._state("thinking")
        await self._reply(text, heard_at, TurnTiming(speech_end_at=heard_at))

    async def _reply(self, text: str, heard_at: float, timing: TurnTiming) -> None:
        if self.before_turn is not None:
            self.before_turn()                 # a ready replacement session takes over HERE
        chunker = SentenceChunker()
        self.voice.resume()          # clear any latched barge-in, or this turn is silent
        self._speaking = True
        self.vad.enter(Mode.SPEAKING)

        async def emit(chunks) -> None:
            for chunk in chunks:
                elapsed = (time.monotonic() - heard_at) * 1000.0
                timing.chunks += 1
                timing.sentences.append(chunk.as_log())
                if timing.first_chunk_ms == 0.0:
                    timing.first_chunk_ms = elapsed
                if self.on_chunk is not None:
                    self.on_chunk(chunk, elapsed)
                await self.voice.say(chunk)
                if timing.first_audio_ms == 0.0:
                    timing.first_audio_ms = (time.monotonic() - heard_at) * 1000.0
                self._state("speaking")

        try:
            async for ev in self.brain.turn(text):  # type: ignore[attr-defined]
                if isinstance(ev, TextDelta):
                    await emit(chunker.push(ev.text))
                elif isinstance(ev, Thinking):
                    timing.thinking_chars += len(ev.text)   # silence the student hears (spec §10)
                elif isinstance(ev, Compacting):
                    # The provider condensing on its own: the slow turn it causes is logged as
                    # what it is (spec §6b), and the caller explains the silence.
                    if not ev.active and ev.duration_ms:
                        timing.compaction_ms = float(ev.duration_ms)
                    if self.on_compacting is not None:
                        self.on_compacting(ev)
                elif isinstance(ev, (ToolCall, ToolOutcome, RateLimited, BrainError)):
                    pass                               # surfaced by the caller's own handlers
                elif isinstance(ev, TurnComplete):
                    timing.ttft_ms, timing.tool_ms = ev.ttft_ms, ev.tool_ms
                    await emit(chunker.close())
                    await self.voice.drain()
                    break
        except asyncio.CancelledError:
            # Barge-in cancelled us. The student is already mid-sentence, so the rest of this
            # reply is not wanted — but the turn still gets recorded, marked, so a cut-short turn
            # never lands in the §10 p90 as if it had completed.
            timing.barged_in = True
            raise
        finally:
            timing.total_ms = (time.monotonic() - heard_at) * 1000.0
            # Playback start, as the voice observed it: only the queue knows when the first
            # sentence actually left for the speakers or the page (SpeechQueue.first_play_at).
            first_play_at = getattr(self.voice, "first_play_at", None)
            if first_play_at is not None:
                timing.first_play_ms = max(0.0, (first_play_at - heard_at) * 1000.0)
            self.timings.append(timing)
            if self.on_turn is not None:
                self.on_turn(timing)
            self._speaking = False
            self.vad.enter(Mode.LISTENING)
            self._state("listening")

    def _state(self, name: str) -> None:
        if self.on_state is not None:
            self.on_state(name)

    def _notice(self, text: str) -> None:
        if self.on_notice is not None:
            self.on_notice(text)
