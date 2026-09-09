"""The voice loop: mic -> VAD -> Whisper -> brain -> VOICEVOX -> speakers (spec §2, M2).

This is the turn cycle of §2 with no browser in front of it yet. The microphone runs on its own
thread because PortAudio reads are blocking; everything else is one asyncio task, so the VAD keeps
seeing frames *while the avatar is speaking* — which is what makes barge-in possible at all.

Turn-taking policy lives here rather than in the VAD (ADR-006): the detector reports what it
hears, this decides what it means given what the tutor is doing.
"""
from __future__ import annotations

import asyncio
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

    _frames: asyncio.Queue | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _mic: threading.Thread | None = field(default=None, init=False)
    _speaking: bool = field(default=False, init=False)
    timings: list[TurnTiming] = field(default_factory=list, init=False)

    # ------------------------------------------------------------------ public
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._frames = asyncio.Queue(maxsize=200)
        self._stop.clear()
        self._mic = threading.Thread(target=self._capture, args=(loop,), daemon=True)
        self._mic.start()
        self._state("listening")
        try:
            while not self._stop.is_set():
                frame = await self._frames.get()
                if frame is None:
                    break
                for event in self.vad.push(frame):
                    await self._handle(event)
        finally:
            self.stop()

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
                    loop.call_soon_threadsafe(self._frames.put_nowait, frame)  # type: ignore[union-attr]
                except (RuntimeError, asyncio.QueueFull):
                    pass          # loop gone, or we are behind: dropping a frame beats blocking
        except audio_mod.AudioUnavailable:
            pass
        finally:
            try:
                loop.call_soon_threadsafe(self._frames.put_nowait, None)  # type: ignore[union-attr]
            except RuntimeError:
                pass

    async def _handle(self, event) -> None:
        if event.kind is EventKind.SPEECH_START and self._speaking:
            # The student is talking over the avatar: stop, drop the rest, take the new turn.
            self.voice.cancel()
            self._speaking = False
            self.vad.enter(Mode.LISTENING)
            if self.on_bargein is not None:
                self.on_bargein()
            self._state("listening")
        elif event.kind is EventKind.SPEECH_END and event.audio is not None:
            await self._turn(event.audio)

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

        async for ev in self.brain.turn(transcript.text):  # type: ignore[attr-defined]
            if isinstance(ev, TextDelta):
                await emit(chunker.push(ev.text))
            elif isinstance(ev, (Thinking, ToolCall, ToolOutcome, RateLimited, BrainError)):
                pass                                   # surfaced by the caller's own handlers
            elif isinstance(ev, TurnComplete):
                await emit(chunker.close())
                await self.voice.drain()
                break

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
