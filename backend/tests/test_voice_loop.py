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
