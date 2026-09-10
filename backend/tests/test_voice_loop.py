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

    def listen(self, audio, quiet_rms=None):
        self.quiet_rms = quiet_rms
        return self.transcript


class FakeVoice:
    def __init__(self):
        self.said: list[Chunk] = []
        self.cancelled = 0
        self.drained = 0
        self.resumed = 0
        self.armed = True

    def resume(self):
        self.resumed += 1
        self.armed = True

    async def say(self, chunk):
        self.said.append(chunk)

    async def drain(self):
        self.drained += 1

    def cancel(self):
        self.cancelled += 1
        self.armed = False        # latches, exactly like the real SpeechQueue


class FakeVad:
    def __init__(self):
        self.mode = Mode.LISTENING
        self.entered: list[Mode] = []
        self.min_speech_ms = 300

    def enter(self, mode):
        self.mode = mode
        self.entered.append(mode)

    def push(self, frame):
        return []


def loop_with(transcript: Transcript, reply="はい。そうですね。"):
    brain, voice, vad = FakeBrain(reply), FakeVoice(), FakeVad()
    states: list[str] = []
    loop = VoiceLoop(turn_mode="vad", brain=brain, stt=FakeStt(transcript), vad=vad, voice=voice,
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
        loop = VoiceLoop(turn_mode="vad", brain=brain, stt=FakeStt(accepted()), vad=vad, voice=voice)
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
        loop = VoiceLoop(turn_mode="vad", brain=brain, stt=FakeStt(accepted()), vad=vad, voice=voice)
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


def test_the_turn_after_a_bargein_is_not_silent():
    """cancel() latches. Nothing but start() ever cleared it, so ONE barge-in muted the tutor
    for the rest of the session: say() dropped every sentence and drain() returned instantly,
    with no error raised anywhere to say so (2026-09-10)."""
    async def scenario():
        gate = asyncio.Event()
        brain, voice, vad = SlowBrain(gate), FakeVoice(), FakeVad()
        loop = VoiceLoop(turn_mode="vad", brain=brain, stt=FakeStt(accepted()), vad=vad, voice=voice)
        end = VadEvent(EventKind.SPEECH_END, 0.0, audio=np.zeros(16000, dtype=np.float32))

        await loop._handle(end)
        await asyncio.sleep(0)
        loop._speaking = True
        await loop._handle(VadEvent(EventKind.SPEECH_START, 0.0))   # barge in
        with pytest.raises(asyncio.CancelledError):
            await loop._turn_task
        assert voice.armed is False, "the queue should be latched shut right after a barge-in"

        # The next thing the student says must produce a speaking tutor again.
        gate.set()
        await loop._handle(end)
        await loop._turn_task
        assert voice.armed is True, "the turn after a barge-in re-arms the queue"
        assert voice.resumed >= 1

    asyncio.run(scenario())


def test_a_second_utterance_during_a_turn_is_ignored_not_stacked():
    async def scenario():
        gate = asyncio.Event()
        brain, voice, vad = SlowBrain(gate), FakeVoice(), FakeVad()
        loop = VoiceLoop(turn_mode="vad", brain=brain, stt=FakeStt(accepted()), vad=vad, voice=voice)
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

# ------------------------------------------------------------- push to talk
def ptt_loop(transcript=None, reply="はい。"):
    brain, voice, vad = FakeBrain(reply), FakeVoice(), FakeVad()
    loop = VoiceLoop(turn_mode="ptt", brain=brain, stt=FakeStt(transcript or accepted()),
                     vad=vad, voice=voice)
    return loop, brain, voice, vad


def speech(seconds=1.0):
    return [np.zeros(512, dtype=np.float32) for _ in range(int(seconds * 16000 / 512))]


def test_in_ptt_mode_the_silence_window_does_not_end_a_turn():
    """The whole point: the VAD stops deciding when the student has finished (spec §9)."""
    async def scenario():
        loop, brain, _, _ = ptt_loop()
        await loop._handle(VadEvent(EventKind.SPEECH_END, 0.0,
                                    audio=np.zeros(16000, dtype=np.float32)))
        assert loop._turn_task is None, "vad ended a turn while the key was in charge"
        assert brain.turns == []
    asyncio.run(scenario())


def test_releasing_the_key_turns_the_buffered_audio_into_a_turn():
    async def scenario():
        loop, brain, _, _ = ptt_loop()
        loop.ptt_begin()
        loop._ptt_buf.extend(speech(1.0))
        loop.ptt_end()
        assert loop._turn_task is not None
        await loop._turn_task
        assert brain.turns == ["こんにちは。"]
    asyncio.run(scenario())


def test_a_stray_tap_is_not_an_utterance():
    """Shorter than the VAD's own minimum speech length: a bumped key, not a sentence."""
    async def scenario():
        loop, brain, _, _ = ptt_loop()
        loop.ptt_begin()
        loop._ptt_buf.extend(speech(0.05))
        loop.ptt_end()
        assert loop._turn_task is None and brain.turns == []
    asyncio.run(scenario())


def test_pressing_while_the_tutor_speaks_is_an_unambiguous_bargein():
    """No threshold guesswork: nobody holds a talk key by accident. This is why ptt is steadier
    than vad on laptop speakers, where the tutor's own voice can trigger the detector."""
    async def scenario():
        gate = asyncio.Event()
        brain, voice, vad = SlowBrain(gate), FakeVoice(), FakeVad()
        loop = VoiceLoop(turn_mode="ptt", brain=brain, stt=FakeStt(accepted()), vad=vad, voice=voice)
        loop.ptt_begin(); loop._ptt_buf.extend(speech(1.0)); loop.ptt_end()
        await asyncio.sleep(0)
        loop._speaking = True

        loop.ptt_begin()                      # interrupt him
        assert voice.cancelled == 1
        with pytest.raises(asyncio.CancelledError):
            await loop._turn_task
        assert loop._speaking is False and vad.mode is Mode.LISTENING

        # and the next release still produces a speaking tutor
        gate.set()
        loop._ptt_buf.extend(speech(1.0)); loop.ptt_end()
        await loop._turn_task
        assert voice.armed is True
    asyncio.run(scenario())


# ------------------------------------------------------------------ new topic (page button)
def test_new_topic_is_a_turn_from_text_not_speech():
    """The page's New topic button: the cue reaches her, and nothing is logged as speech."""
    loop, brain, voice, vad, states = loop_with(accepted())

    async def run():
        loop.ask("（話題を変えて）")
        await loop._turn_task

    asyncio.run(run())
    assert brain.turns == ["（話題を変えて）"]
    assert [c.text for c in voice.said] == ["はい。", "そうですね。"]
    assert loop.timings[-1].transcript == ""          # the student said nothing
    assert states[-1] == "listening"


def test_new_topic_interrupts_a_reply_in_flight():
    async def run():
        gate = asyncio.Event()
        brain, voice, vad = SlowBrain(gate), FakeVoice(), FakeVad()
        loop = VoiceLoop(turn_mode="ptt", brain=brain, stt=FakeStt(accepted()), vad=vad, voice=voice)
        first = asyncio.create_task(loop._turn(np.zeros(16000, dtype=np.float32)))
        loop._turn_task = first
        await asyncio.sleep(0.01)                     # she is still working on the first one
        loop.ask("（話題を変えて）")
        with pytest.raises(asyncio.CancelledError):
            await first
        gate.set()                                    # let the new-topic turn answer
        await loop._turn_task
        return brain, voice

    brain, voice = asyncio.run(run())
    assert voice.cancelled >= 1
    assert brain.turns == ["こんにちは。", "（話題を変えて）"]


# ------------------------------------------------------- quiet, for THIS microphone
def test_quiet_is_measured_from_the_room_between_turns():
    from backend import audio as audio_mod
    from backend.stt import QUIET_RMS
    loop, *_ = loop_with(accepted())
    assert loop.quiet_rms() == QUIET_RMS                 # nothing measured yet: the old default
    loop._floor = 0.00002                                # a noise-suppressed headset at rest
    assert loop.quiet_rms() == pytest.approx(audio_mod.SILENT_RMS * 2)
    loop._floor = 0.01                                   # a noisy room
    assert loop.quiet_rms() == QUIET_RMS                 # never stricter than before


def test_the_turn_hands_the_measured_level_to_the_stt():
    loop, *_ = loop_with(accepted())
    loop._floor = 0.001
    asyncio.run(loop._turn(np.zeros(16000, dtype=np.float32)))
    assert loop.stt.quiet_rms == pytest.approx(0.004)
