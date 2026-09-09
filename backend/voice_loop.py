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
from backend.brain import BrainError, RateLimited, TextDelta, Thinking, ToolCall, ToolOutcome, TurnComplete
from backend.chunker import SentenceChunker
from backend.speaker import SpeechQueue
from backend.stt import SpeechToText, Transcript
from backend.vad import FRAME_SAMPLES, EventKind, Mode, VoiceActivityDetector


def vad_frame_samples() -> int:
    return FRAME_SAMPLES


@dataclass
class TurnTiming:
    """Voice-to-voice, broken down (spec §10). Recorded per turn so p90 is measurable."""

    speech_end_at: float
    stt_ms: float = 0.0
    first_chunk_ms: float = 0.0
    first_audio_ms: float = 0.0
    total_ms: float = 0.0
    transcript: str = ""
    chunks: int = 0
    #: The student talked over this turn, so it was cut short. Recorded, but never counted in the
    #: §10 p90 as a completed turn — it did not fail to be fast, it was interrupted.
    barged_in: bool = False

    def voice_to_voice_ms(self) -> float:
        return self.first_audio_ms


@dataclass
class VoiceLoop:
    """One live conversation. `run()` until the user stops it."""

    brain: object
    stt: SpeechToText
    vad: VoiceActivityDetector
    voice: SpeechQueue
    input_device: str | int | None = None
    on_state: Callable[[str], None] | None = None
    on_transcript: Callable[[Transcript], None] | None = None
    on_chunk: Callable[[object, float], None] | None = None
    on_turn: Callable[[TurnTiming], None] | None = None
    on_bargein: Callable[[], None] | None = None
    #: (level, speech probability) per frame — so the user can SEE that they are being heard.
    on_level: Callable[[float, float], None] | None = None

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
                events = self.vad.push(frame)
                if self.on_level is not None and not self._speaking:
                    self.on_level(float(np.sqrt(np.mean(np.square(frame)))), self.vad.last_probability)
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

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------- inner
    def _capture(self, loop: asyncio.AbstractEventLoop) -> None:
        """Mic thread: PortAudio reads are blocking, so they never touch the event loop."""
        try:
            # Frames must be exactly Silero's window (512 samples @ 16 kHz); the VAD is stateful
            # and a different size silently degrades its judgement rather than erroring.
            for frame in audio_mod.capture(self.input_device, frame_samples=vad_frame_samples()):
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
        if event.kind is EventKind.SPEECH_START and self._speaking:
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
            if self._turn_task is not None and not self._turn_task.done():
                return                       # a turn is already in flight; one at a time
            self._turn_task = asyncio.create_task(self._turn(event.audio))

    async def _turn(self, audio: np.ndarray) -> None:
        heard_at = time.monotonic()
        self._state("thinking")

        started = time.monotonic()
        transcript = await asyncio.to_thread(self.stt.listen, audio)
        timing = TurnTiming(speech_end_at=heard_at, stt_ms=(time.monotonic() - started) * 1000.0,
                            transcript=transcript.text)
        if self.on_transcript is not None:
            self.on_transcript(transcript)
        if not transcript:
            self._state("listening")
            self.vad.enter(Mode.LISTENING)
            return

        chunker = SentenceChunker()
        self.voice.resume()          # clear any latched barge-in, or this turn is silent
        self._speaking = True
        self.vad.enter(Mode.SPEAKING)

        async def emit(chunks) -> None:
            for chunk in chunks:
                elapsed = (time.monotonic() - heard_at) * 1000.0
                timing.chunks += 1
                if timing.first_chunk_ms == 0.0:
                    timing.first_chunk_ms = elapsed
                if self.on_chunk is not None:
                    self.on_chunk(chunk, elapsed)
                await self.voice.say(chunk)
                if timing.first_audio_ms == 0.0:
                    timing.first_audio_ms = (time.monotonic() - heard_at) * 1000.0
                self._state("speaking")

        try:
            async for ev in self.brain.turn(transcript.text):  # type: ignore[attr-defined]
                if isinstance(ev, TextDelta):
                    await emit(chunker.push(ev.text))
                elif isinstance(ev, (Thinking, ToolCall, ToolOutcome, RateLimited, BrainError)):
                    pass                               # surfaced by the caller's own handlers
                elif isinstance(ev, TurnComplete):
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
            self.timings.append(timing)
            if self.on_turn is not None:
                self.on_turn(timing)
            self._speaking = False
            self.vad.enter(Mode.LISTENING)
            self._state("listening")

    def _state(self, name: str) -> None:
        if self.on_state is not None:
            self.on_state(name)
