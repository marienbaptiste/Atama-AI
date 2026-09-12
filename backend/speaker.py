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
    #: When playback started, as close as this side can observe it: the local stream has just
    #: been handed the samples, or the browser has just been sent them (the page does not report
    #: back, so the WebSocket hop and the decode are not in it — tens of ms, not hundreds).
    played_at: float | None = None
    error: str = ""


@dataclass
class SpeechQueue:
    """Synthesise -> play, in order, without blocking the conversation loop."""

    tts: VoicevoxClient
    device: str | int | None = None
    #: Where a synthesised sentence GOES. None plays it on this machine's speakers; set it and the
    #: audio is handed elsewhere instead — the browser, over the WebSocket — and the queue simply
    #: waits out its duration so drain(), barge-in and the §10 timings keep working unchanged.
    #: async (Speech) -> seconds of audio.
    sink: object = None
    on_event: Callable[[str, SpokenChunk], None] | None = None
    #: `played_at` of the first sentence played since the last `resume()` — the turn's true
    #: voice-to-voice moment (spec §10). None until something has started playing this turn.
    first_play_at: float | None = field(default=None, init=False)
    _player: audio_mod.Player | None = field(default=None, init=False, repr=False)
    _queue: asyncio.Queue | None = field(default=None, init=False)
    _task: asyncio.Task | None = field(default=None, init=False)
    _cancelled: bool = field(default=False, init=False)
    #: Set by `cancel()`. The sink path holds the queue for a sentence's duration by waiting on
    #: this, so a barge-in wakes it at once instead of letting it sleep through the cut sentence
    #: — which delayed the next turn's first audio by up to a whole sentence (2026-09-12).
    _interrupt: asyncio.Event | None = field(default=None, init=False, repr=False)

    async def start(self) -> None:
        self._queue = asyncio.Queue()
        self._interrupt = asyncio.Event()
        self._cancelled = False
        self.first_play_at = None
        # One output stream for the session: a stream opened per sentence drops its own first
        # ~100 ms while starting, which clipped every opening syllable (fixed 2026-09-09).
        if self.sink is not None:
            self._task = asyncio.create_task(self._play_loop())
            return                        # no local audio device at all when the browser has it
        self._player = audio_mod.Player(self.device)
        # Open it now, not on the first sentence: an unopened stream eats its own first ~100 ms,
        # which clipped the tutor's opening syllable (2026-09-09).
        try:
            await asyncio.to_thread(self._player.open)
        except Exception:  # noqa: BLE001 - a device that will not pre-open may still play
            pass
        self._task = asyncio.create_task(self._play_loop())

    def set_device(self, device: str | int | None) -> None:
        """Switch local playback to another output, live. Nothing to do when the browser plays."""
        self.device = device
        if self._player is not None:
            self._player.switch(device)

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
        # The study marks ride with the audio to the page (ADR-036); the voice never had them.
        speech.grammar, speech.target = getattr(chunk, "grammar", ()), getattr(chunk, "target", "")
        speech.used = getattr(chunk, "used", "")
        record = SpokenChunk(chunk, speech.synth_ms, started)
        self._emit("synthesised", record)
        await self._queue.put((speech, record))

    async def drain(self) -> None:
        """Wait for everything queued to finish playing (end of turn)."""
        if self._queue is not None and not self._cancelled:
            await self._queue.join()

    def resume(self) -> None:
        """Re-arm after a barge-in. MUST be called at the start of every turn.

        `cancel()` latches `_cancelled`, and only `start()` ever cleared it — so one barge-in
        left say() dropping every later sentence and drain() returning instantly, muting the
        tutor for the rest of the session with no error anywhere (2026-09-10).
        """
        self._cancelled = False
        self.first_play_at = None
        if self._interrupt is not None:
            self._interrupt.clear()

    def cancel(self) -> None:
        """Barge-in: stop the current sentence and drop the rest. Latches until resume()."""
        self._cancelled = True
        if self._player is not None:
            self._player.cancel()
        if self._interrupt is not None:
            self._interrupt.set()         # wake the sink path out of the cut sentence, now
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
                if self._cancelled:
                    pass
                elif self.sink is not None:
                    seconds = await self.sink(speech)
                    # Stamped AFTER the hand-over: the audio has left for the page, so this is
                    # when it starts playing there, give or take the hop — not when we decided
                    # to send it, which could be a whole autoplay wait earlier.
                    self._started(record)
                    # The browser is playing it, so hold the queue for as long as that takes.
                    # Without this every sentence would be dispatched at once and the tutor would
                    # talk over herself. The hold ends early on cancel(): barge-in working.
                    await self._hold(max(0.0, float(seconds or 0.0)))
                elif self._player is not None:
                    self._started(record)
                    await asyncio.to_thread(self._player.play, speech.wav)
            except Exception as exc:  # noqa: BLE001 - PortAudioError is neither of the old two,
                # and one sentence that cannot play must not end playback for the session.
                record.error = f"playback failed: {exc}"
                self._emit("error", record)
            finally:
                self._queue.task_done()

    async def _hold(self, seconds: float) -> None:
        """Wait out a sentence playing elsewhere — or return the moment `cancel()` is called."""
        if seconds <= 0.0:
            return
        if self._interrupt is None or self._interrupt.is_set():
            return
        try:
            await asyncio.wait_for(self._interrupt.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass                          # the sentence played to its end

    def _started(self, record: SpokenChunk) -> None:
        record.played_at = time.monotonic()
        if self.first_play_at is None:
            self.first_play_at = record.played_at
        self._emit("playing", record)

    def _emit(self, kind: str, record: SpokenChunk) -> None:
        if self.on_event is not None:
            self.on_event(kind, record)
