"""Speech queue: synthesise sentence N+1 while sentence N is still playing (spec §8, ADR-008).

The chunker hands us a sentence the moment it closes. Synthesising and playing it inline would
serialise the pipeline and waste the whole point of streaming, so synthesis and playback each run
on their own worker and hand over through a queue.

Barge-in (spec §8) calls `cancel()`: playback stops immediately and everything still queued is
dropped, because the student is talking now and the rest of the answer is no longer wanted.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable

from backend import audio as audio_mod
from backend.chunker import Chunk
from backend.tts_voicevox import Speech, VoicevoxClient, VoicevoxError


@dataclass
class SpokenChunk:
    """One sentence that made it all the way to the speakers."""

    chunk: Chunk
    synth_ms: float
    queued_at: float
    played_at: float | None = None
    error: str = ""


@dataclass
class SpeechQueue:
    """Synthesise -> play, in order, without blocking the conversation loop."""

    tts: VoicevoxClient
    device: str | int | None = None
    on_event: Callable[[str, SpokenChunk], None] | None = None
    _player: audio_mod.Player | None = field(default=None, init=False, repr=False)
    _queue: asyncio.Queue | None = field(default=None, init=False)
    _task: asyncio.Task | None = field(default=None, init=False)
    _cancelled: bool = field(default=False, init=False)

    async def start(self) -> None:
        self._queue = asyncio.Queue()
        self._cancelled = False
        # One output stream for the session: a stream opened per sentence drops its own first
        # ~100 ms while starting, which clipped every opening syllable (fixed 2026-09-09).
        self._player = audio_mod.Player(self.device)
        self._task = asyncio.create_task(self._play_loop())

    async def say(self, chunk: Chunk) -> None:
        """Synthesise one sentence and queue it. Returns as soon as the audio exists."""
        if self._queue is None:
            await self.start()
        started = time.monotonic()
        try:
            speech: Speech = await asyncio.to_thread(self.tts.say, chunk.text, chunk.emotion)
        except VoicevoxError as exc:
            self._emit("error", SpokenChunk(chunk, (time.monotonic() - started) * 1000, started, error=str(exc)))
            return
        if self._cancelled:
            return
        record = SpokenChunk(chunk, speech.synth_ms, started)
        self._emit("synthesised", record)
        await self._queue.put((speech, record))

    async def drain(self) -> None:
        """Wait for everything queued to finish playing (end of turn)."""
        if self._queue is not None and not self._cancelled:
            await self._queue.join()

    def cancel(self) -> None:
        """Barge-in: stop the current sentence and drop the rest."""
        self._cancelled = True
        if self._player is not None:
            self._player.cancel()
        if self._queue is not None:
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    break

    async def aclose(self) -> None:
        self.cancel()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._player is not None:
            self._player.close()
            self._player = None

    # ------------------------------------------------------------------ inner
    async def _play_loop(self) -> None:
        assert self._queue is not None
        while True:
            speech, record = await self._queue.get()
            try:
                if not self._cancelled and self._player is not None:
                    record.played_at = time.monotonic()
                    self._emit("playing", record)
                    await asyncio.to_thread(self._player.play, speech.wav)
            except (audio_mod.AudioUnavailable, OSError) as exc:
                record.error = f"playback failed: {exc}"
                self._emit("error", record)
            finally:
                self._queue.task_done()

    def _emit(self, kind: str, record: SpokenChunk) -> None:
        if self.on_event is not None:
            self.on_event(kind, record)
