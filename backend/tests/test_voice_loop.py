"""The voice loop: transcript -> turn -> speech, and barge-in (spec §2, ADR-018)."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from backend.brain import TextDelta, TurnComplete
from backend.chunker import Chunk
from backend.stt import Transcript
from backend.vad import EventKind, Mode, VadEvent
from backend.voice_loop import VoiceLoop


class FakeBrain:
    name = "fake"

    def __init__(self, reply="はい。そうですね。"):
        self.reply = reply
        self.turns: list[str] = []

    async def turn(self, text):
        self.turns.append(text)
        for piece in self.reply:
            yield TextDelta(piece)
        yield TurnComplete(text=self.reply)


class FakeStt:
    def __init__(self, transcript: Transcript):
        self.transcript = transcript

    def listen(self, audio):
        return self.transcript


class FakeVoice:
    def __init__(self):
        self.said: list[Chunk] = []
        self.cancelled = 0
        self.drained = 0

    async def say(self, chunk):
        self.said.append(chunk)

    async def drain(self):
        self.drained += 1

    def cancel(self):
        self.cancelled += 1


class FakeVad:
    def __init__(self):
        self.mode = Mode.LISTENING
        self.entered: list[Mode] = []

    def enter(self, mode):
        self.mode = mode
        self.entered.append(mode)

    def push(self, frame):
        return []


def loop_with(transcript: Transcript, reply="はい。そうですね。"):
    brain, voice, vad = FakeBrain(reply), FakeVoice(), FakeVad()
    states: list[str] = []
    loop = VoiceLoop(brain=brain, stt=FakeStt(transcript), vad=vad, voice=voice,
                     on_state=states.append)
    return loop, brain, voice, vad, states


def accepted(text="こんにちは。"):
    return Transcript(text=text, accepted=True)


def test_an_accepted_utterance_becomes_a_spoken_turn():
    loop, brain, voice, vad, states = loop_with(accepted())
    asyncio.run(loop._turn(np.zeros(16000, dtype=np.float32)))
    assert brain.turns == ["こんにちは。"]
    assert [c.text for c in voice.said] == ["はい。", "そうですね。"]
    assert voice.drained == 1
    assert "thinking" in states and "speaking" in states and states[-1] == "listening"


def test_a_discarded_transcript_never_reaches_the_brain():
    """A Whisper hallucination must not make the tutor answer something unsaid (spec §9)."""
    rejected = Transcript(text="ご視聴ありがとうございました", accepted=False, reason="blocklisted")
    loop, brain, voice, vad, states = loop_with(rejected)
    asyncio.run(loop._turn(np.zeros(16000, dtype=np.float32)))
    assert brain.turns == [] and voice.said == []
    assert vad.mode is Mode.LISTENING


def test_the_vad_is_told_when_the_avatar_is_speaking():
    loop, brain, voice, vad, states = loop_with(accepted())
    asyncio.run(loop._turn(np.zeros(16000, dtype=np.float32)))
    assert Mode.SPEAKING in vad.entered and vad.entered[-1] is Mode.LISTENING


def test_timings_are_recorded_for_every_turn():
    loop, *_ = loop_with(accepted())
    asyncio.run(loop._turn(np.zeros(16000, dtype=np.float32)))
    t = loop.timings[0]
    assert t.transcript == "こんにちは。" and t.chunks == 2
    assert t.first_chunk_ms > 0 and t.voice_to_voice_ms() > 0 and t.total_ms >= t.first_chunk_ms


def test_speech_while_speaking_is_a_bargein():
    loop, brain, voice, vad, states = loop_with(accepted())
    barged = []
    loop.on_bargein = lambda: barged.append(True)
    loop._speaking = True
    asyncio.run(loop._handle(VadEvent(EventKind.SPEECH_START, 0.0)))
    assert voice.cancelled == 1 and barged == [True]
    assert loop._speaking is False and vad.mode is Mode.LISTENING


def test_speech_while_listening_is_not_a_bargein():
    loop, brain, voice, vad, states = loop_with(accepted())
    loop._speaking = False
    asyncio.run(loop._handle(VadEvent(EventKind.SPEECH_START, 0.0)))
    assert voice.cancelled == 0


# ------------------------------------------- the turn must not block the ears
class SlowBrain(FakeBrain):
    """A brain that takes a while, like a real one."""

    def __init__(self, gate: asyncio.Event, reply="はい。"):
        super().__init__(reply)
        self.gate = gate

    async def turn(self, text):
        self.turns.append(text)
        await self.gate.wait()          # held open until the test lets go
        for piece in self.reply:
            yield TextDelta(piece)
        yield TurnComplete(text=self.reply)


def test_handling_an_utterance_does_not_block_on_the_turn():
    """Regression, 2026-09-09. `_turn` used to be awaited inline in the consume loop, all the way
    through `voice.drain()`. For the whole turn nothing drained the frame queue, so it overflowed
    (QueueFull, once per frame, ~50/s) and — far worse — the VAD saw no audio while the avatar was
    speaking, which made barge-in impossible. The module docstring promised the opposite."""
    async def scenario():
        gate = asyncio.Event()
        brain, voice, vad = SlowBrain(gate), FakeVoice(), FakeVad()
        loop = VoiceLoop(brain=brain, stt=FakeStt(accepted()), vad=vad, voice=voice)
        await asyncio.wait_for(
            loop._handle(VadEvent(EventKind.SPEECH_END, 0.0, audio=np.zeros(16000, dtype=np.float32))),
            timeout=1.0)                # returns immediately, while the brain is still stuck
        assert loop._turn_task is not None and not loop._turn_task.done()
        gate.set()
        await loop._turn_task
        assert [c.text for c in voice.said] == ["はい。"]

    asyncio.run(scenario())


def test_a_bargein_cancels_the_running_turn_and_marks_it():
    async def scenario():
        gate = asyncio.Event()
        brain, voice, vad = SlowBrain(gate), FakeVoice(), FakeVad()
        loop = VoiceLoop(brain=brain, stt=FakeStt(accepted()), vad=vad, voice=voice)
        await loop._handle(VadEvent(EventKind.SPEECH_END, 0.0, audio=np.zeros(16000, dtype=np.float32)))
        await asyncio.sleep(0)          # let the task actually start
        loop._speaking = True
        await loop._handle(VadEvent(EventKind.SPEECH_START, 0.0))
        assert voice.cancelled == 1
        with pytest.raises(asyncio.CancelledError):
            await loop._turn_task
        # Recorded, and marked: an interrupted turn was not slow, so it must not sit in the p90.
        assert loop.timings[-1].barged_in is True
        assert loop._speaking is False and vad.mode is Mode.LISTENING

    asyncio.run(scenario())


def test_a_second_utterance_during_a_turn_is_ignored_not_stacked():
    async def scenario():
        gate = asyncio.Event()
        brain, voice, vad = SlowBrain(gate), FakeVoice(), FakeVad()
        loop = VoiceLoop(brain=brain, stt=FakeStt(accepted()), vad=vad, voice=voice)
        end = VadEvent(EventKind.SPEECH_END, 0.0, audio=np.zeros(16000, dtype=np.float32))
        await loop._handle(end)
        await asyncio.sleep(0)          # let the task actually start
        first = loop._turn_task
        await loop._handle(end)
        assert loop._turn_task is first and len(brain.turns) == 1
        gate.set()
        await loop._turn_task

    asyncio.run(scenario())


def test_a_full_frame_queue_drops_the_oldest_frame_and_never_raises():
    """`call_soon_threadsafe` only schedules `put_nowait`, so a QueueFull raised inside it lands in
    a bare asyncio handle where nothing catches it — one traceback per dropped frame."""
    async def scenario():
        loop, *_ = loop_with(accepted())
        loop._frames = asyncio.Queue(maxsize=2)
        for i in range(5):
            loop._offer(np.full(4, i, dtype=np.float32))     # must not raise
        assert loop._frames.qsize() == 2 and loop.dropped_frames == 3
        newest = [loop._frames.get_nowait()[0] for _ in range(2)]
        assert newest == [3.0, 4.0]                          # the stale audio is what got dropped

    asyncio.run(scenario())
