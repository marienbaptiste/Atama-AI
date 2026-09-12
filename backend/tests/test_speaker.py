"""The speech queue: order, barge-in, the sink hold, drain (spec §8, ADR-008).

Hermetic: a fake TTS that returns an empty Speech at once, and a fake sink standing in for the
browser. No audio device is opened — the sink path never constructs a Player.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.chunker import Chunk
from backend.speaker import SpeechQueue
from backend.tts_voicevox import Speech, VoicevoxError
from backend.visemes import VisemeTimeline


class FakeTts:
    def __init__(self, fail_on: str = ""):
        self.fail_on = fail_on
        self.said: list[str] = []

    def say(self, text: str, emotion: str = "") -> Speech:
        if text == self.fail_on:
            raise VoicevoxError("engine says no")
        self.said.append(text)
        return Speech(text=text, emotion=emotion, wav=b"", style_id=1, synth_ms=1.0,
                      timeline=VisemeTimeline([], [], [], [], 0.0))


class FakeSink:
    """The browser: records what arrives and when, and reports each sentence's duration."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.got: list[tuple[str, float]] = []

    async def __call__(self, speech: Speech) -> float:
        self.got.append((speech.text, time.monotonic()))
        return self.seconds


def queue(seconds: float = 0.05, tts: FakeTts | None = None) -> tuple[SpeechQueue, FakeSink, list]:
    sink = FakeSink(seconds)
    events: list[tuple[str, str]] = []
    q = SpeechQueue(tts or FakeTts(), sink=sink, on_event=lambda kind, rec: events.append((kind, rec.chunk.text)))
    return q, sink, events


async def until(pred, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, "condition never came true"
        await asyncio.sleep(0.005)


# ------------------------------------------------------------------- ordering
def test_sentences_reach_the_sink_in_order_one_at_a_time():
    async def scenario():
        q, sink, events = queue(seconds=0.05)
        await q.start()
        await q.say(Chunk("一。"))
        await q.say(Chunk("二。"))
        await q.say(Chunk("三。"))
        await q.drain()
        assert [t for t, _ in sink.got] == ["一。", "二。", "三。"]
        # Each sentence is held for its duration before the next one is dispatched, or the
        # browser is handed three sentences at once and she talks over herself.
        gaps = [b - a for (_, a), (_, b) in zip(sink.got, sink.got[1:])]
        assert all(g >= 0.045 for g in gaps), gaps
        assert [k for k, _ in events] == ["synthesised", "synthesised", "synthesised",
                                          "playing", "playing", "playing"] or \
               [k for k, t in events if t == "一。"] == ["synthesised", "playing"]
        await q.aclose()

    asyncio.run(scenario())


def test_drain_waits_for_the_last_sentence_to_finish_playing():
    async def scenario():
        q, sink, _ = queue(seconds=0.08)
        await q.start()
        started = time.monotonic()
        await q.say(Chunk("一。"))
        await q.say(Chunk("二。"))
        await q.drain()
        assert time.monotonic() - started >= 0.15          # two holds, back to back
        assert len(sink.got) == 2
        await q.aclose()

    asyncio.run(scenario())


def test_a_synthesis_failure_is_reported_and_the_rest_still_plays():
    async def scenario():
        q, sink, events = queue(seconds=0.01, tts=FakeTts(fail_on="二。"))
        await q.start()
        for t in ("一。", "二。", "三。"):
            await q.say(Chunk(t))
        await q.drain()
        assert [t for t, _ in sink.got] == ["一。", "三。"]
        assert ("error", "二。") in events
        await q.aclose()

    asyncio.run(scenario())


# ------------------------------------------------------------------- barge-in
def test_cancel_wakes_the_sink_hold_instead_of_sleeping_through_the_cut_sentence():
    """Regression, 2026-09-12. The sink path held the queue with a plain sleep for the whole
    sentence. cancel() drained the queue but never woke that sleep, so after a barge-in the
    next turn's first sentence waited behind a sentence nobody was hearing any more."""
    async def scenario():
        q, sink, _ = queue(seconds=5.0)                  # a long sentence
        await q.start()
        await q.say(Chunk("長い文です。"))
        await until(lambda: len(sink.got) == 1)
        q.cancel()                                       # the student talks over her
        q.resume()                                       # the next turn begins
        started = time.monotonic()
        await q.say(Chunk("次の文。"))
        await until(lambda: len(sink.got) == 2, timeout=1.0)
        assert time.monotonic() - started < 0.5, "the next turn waited behind the cut sentence"
        await q.aclose()

    asyncio.run(scenario())


def test_cancel_drops_everything_queued_and_latches_until_resume():
    async def scenario():
        q, sink, _ = queue(seconds=0.2)
        await q.start()
        await q.say(Chunk("一。"))
        await q.say(Chunk("二。"))
        await q.say(Chunk("三。"))
        await until(lambda: len(sink.got) == 1)
        q.cancel()
        await q.say(Chunk("四。"))                       # latched: dropped, not queued
        await q.drain()                                  # returns at once while cancelled
        await asyncio.sleep(0.05)
        assert [t for t, _ in sink.got] == ["一。"]
        q.resume()
        await q.say(Chunk("五。"))
        await q.drain()
        assert [t for t, _ in sink.got] == ["一。", "五。"]
        await q.aclose()

    asyncio.run(scenario())


def test_cancel_before_start_is_harmless():
    q, _, _ = queue()
    q.cancel()
    q.resume()


# --------------------------------------------------------- playback start (spec §10)
def test_first_play_at_is_stamped_when_the_audio_leaves_and_reset_per_turn():
    """`played_at` is taken AFTER the sink hands the audio over — the sink may wait for the page
    to be clicked first — so it is the closest thing to playback start this side can see."""
    async def scenario():
        q, sink, events = queue(seconds=0.02)
        await q.start()
        assert q.first_play_at is None
        await q.say(Chunk("一。"))
        await q.say(Chunk("二。"))
        await q.drain()
        assert q.first_play_at is not None
        assert q.first_play_at >= sink.got[0][1]           # not before the hand-over
        assert q.first_play_at < sink.got[1][1]            # and it is the FIRST sentence's
        assert events.count(("playing", "一。")) == 1
        q.resume()
        assert q.first_play_at is None                     # a new turn measures afresh
        await q.aclose()

    asyncio.run(scenario())


def test_aclose_stops_the_play_loop():
    async def scenario():
        q, _, _ = queue(seconds=5.0)
        await q.start()
        await q.say(Chunk("一。"))
        await asyncio.wait_for(q.aclose(), timeout=1.0)
        assert q._task is None

    asyncio.run(scenario())


@pytest.mark.parametrize("seconds", [0.0, -1.0, None])
def test_a_sink_that_reports_no_duration_does_not_hold(seconds):
    async def scenario():
        q, sink, _ = queue()
        sink.seconds = seconds
        await q.start()
        started = time.monotonic()
        await q.say(Chunk("一。"))
        await q.drain()
        assert time.monotonic() - started < 0.5
        await q.aclose()

    asyncio.run(scenario())
