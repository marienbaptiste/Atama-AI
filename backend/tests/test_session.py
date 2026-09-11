"""Pre-emptive rotation (ADR-032, ROADMAP 18 "Test — rotation, hermetic"). Fake brains only.

The contract under test: rotation arms above the threshold; the swap happens only at a turn
boundary; a replacement that is not ready (or fails) keeps the old session and tries again; the
old process is closed only after the new one has taken a turn.
"""
from __future__ import annotations

import asyncio

from backend import prompt
from backend import session as session_api


class FakeBrain:
    def __init__(self, name, used=None, window=200_000):
        self.name, self.closed = name, False
        self.meters = {"context_tokens": used, "context_window": window}

    async def aclose(self):
        self.closed = True


def rotator(threshold=0.8, spawn=None, handoff="STUDENT: こんにちは\nTUTOR: はい。"):
    spawned, logs = [], []

    async def default_spawn(brief):
        spawned.append(brief)
        return FakeBrain(f"new{len(spawned)}")

    r = session_api.Rotator(threshold, spawn or default_spawn, lambda: handoff, logs.append)
    return r, spawned, logs


def test_it_arms_only_above_the_threshold():
    r, _, _ = rotator()
    assert not r.observe(FakeBrain("old", used=150_000))           # 75 % of 200k
    assert r.observe(FakeBrain("old", used=160_000))               # 80 %


def test_zero_means_never_rotate():
    r, _, _ = rotator(threshold=0)
    assert not r.observe(FakeBrain("old", used=199_000)) and not r.enabled


def test_unknown_context_never_arms():
    r, _, _ = rotator()
    assert not r.observe(FakeBrain("old", used=None))


def test_the_swap_happens_only_at_a_turn_boundary_and_carries_the_handoff():
    async def run():
        r, spawned, _ = rotator()
        old = FakeBrain("old", used=170_000)
        r.observe(old)
        r.prepare()
        assert r.take(old) is None                                  # not ready: keep the old one
        await asyncio.sleep(0)                                      # the replacement starts
        await asyncio.sleep(0)
        new = r.take(old)
        assert new is not None and new.name == "new1"
        assert spawned == ["STUDENT: こんにちは\nTUTOR: はい。"]      # the lesson so far went with it
        assert not old.closed                                       # not until the new one has spoken
        await r.settle()
        assert old.closed and r.rotations == 1 and not r.armed
    asyncio.run(run())


def test_prepare_is_started_once_however_often_it_is_asked():
    async def run():
        r, spawned, _ = rotator()
        r.observe(FakeBrain("old", used=190_000))
        for _ in range(5):
            r.prepare()
        await asyncio.sleep(0.01)
        assert len(spawned) == 1
    asyncio.run(run())


def test_a_failed_replacement_keeps_the_old_session_and_gives_up_after_three():
    async def run():
        async def broken(brief):
            raise RuntimeError("spawn failed")

        r, _, logs = rotator(spawn=broken)
        old = FakeBrain("old", used=190_000)
        for _ in range(session_api.MAX_FAILURES):
            r.observe(old)
            r.prepare()
            await asyncio.sleep(0.01)
            assert r.take(old) is None and not old.closed           # the conversation continues
        assert r.failures == session_api.MAX_FAILURES and not r.enabled
        assert any("giving up" in line for line in logs)
        assert not r.observe(old)                                    # no more attempts this session
    asyncio.run(run())


def test_a_replacement_built_for_a_stale_tutor_is_closed_not_used():
    async def run():
        release = asyncio.Event()
        made = []

        async def slow(brief):
            await release.wait()
            made.append(FakeBrain("late"))
            return made[-1]

        r, _, _ = rotator(spawn=slow)
        r.observe(FakeBrain("old", used=190_000))
        r.prepare()
        await asyncio.sleep(0)
        await r.discard()                                            # the tutor changed meanwhile
        release.set()
        await asyncio.sleep(0.01)
        assert r.take(FakeBrain("old")) is None
    asyncio.run(run())


def test_discard_closes_a_ready_replacement():
    async def run():
        r, _, _ = rotator()
        r.observe(FakeBrain("old", used=190_000))
        r.prepare()
        await asyncio.sleep(0.01)
        ready = r._ready
        await r.discard()
        assert ready.closed and r.take(FakeBrain("old")) is None
    asyncio.run(run())


# ---------------------------------------------------------------- the handoff in the prompt
def test_the_handoff_follows_memory_within_its_own_budget():
    p = prompt.build("WaniKani level 4.", memory="LAST SESSION\n台風の話。", handoff="STUDENT: 元気です\nTUTOR: よかった。")
    assert "EARLIER IN THIS LESSON" in p.text and "STUDENT: 元気です" in p.text
    assert p.text.index("LAST SESSION") < p.text.index("EARLIER IN THIS LESSON")
    assert p.sections["handoff"] <= prompt.HANDOFF_MAX_TOKENS


def test_no_handoff_leaves_no_heading():
    assert "EARLIER IN THIS LESSON" not in prompt.build("WaniKani level 4.").text


def test_a_long_handoff_is_cut_and_reported():
    nl = chr(10)
    p = prompt.build("x", handoff=nl.join(["STUDENT: " + "話" * 60] * 80))
    assert "handoff" in p.truncated and p.sections["handoff"] <= prompt.HANDOFF_MAX_TOKENS


def test_a_requested_rotation_happens_even_with_automatic_rotation_off():
    """The resync button (spec §5b): new study data reaches her through a fresh session."""
    async def run():
        r, spawned, _ = rotator(threshold=0)
        await r.rotate_next_turn("study data refreshed")
        await asyncio.sleep(0.01)
        old = FakeBrain("old")
        assert r.take(old) is not None and spawned
    asyncio.run(run())


def test_a_request_replaces_a_replacement_built_on_old_data():
    async def run():
        r, spawned, _ = rotator()
        r.observe(FakeBrain("old", used=190_000))
        r.prepare()
        await asyncio.sleep(0.01)
        stale = r._ready
        await r.rotate_next_turn("study data refreshed")
        await asyncio.sleep(0.01)
        assert stale.closed and r.take(FakeBrain("old")) is not stale and len(spawned) == 2
    asyncio.run(run())


# ------------------------------------- the provider's compaction point is its policy, not ours
def test_an_automatic_compaction_before_the_threshold_lowers_it():
    r, _, logs = rotator(threshold=0.7)
    assert r.learn("auto", 100_000, 200_000) == 0.425              # 85 % of where it compacted
    assert r.threshold == 0.425 and any("compacted on its own" in line for line in logs)
    assert r.observe(FakeBrain("old", used=90_000))                # now arms well before 100k


def test_a_compaction_someone_asked_for_teaches_nothing():
    r, _, _ = rotator(threshold=0.7)
    assert r.learn("manual", 20_000, 200_000) is None and r.threshold == 0.7


def test_a_compaction_later_than_the_threshold_teaches_nothing():
    r, _, _ = rotator(threshold=0.5)
    assert r.learn("auto", 190_000, 200_000) is None and r.threshold == 0.5


def test_learning_never_switches_rotation_on():
    r, _, _ = rotator(threshold=0)
    assert r.learn("auto", 50_000, 200_000) is None and not r.enabled


def test_a_learned_threshold_has_a_floor():
    r, _, _ = rotator(threshold=0.7)
    assert r.learn("auto", 2_000, 200_000) == session_api.MIN_LEARNED


def test_the_learned_threshold_survives_a_restart(tmp_path):
    path = tmp_path / session_api.LEARNED_FILE
    assert session_api.load_learned(path) is None                  # nothing learned yet
    session_api.save_learned(path, 0.425, pre_tokens=100_000, window=200_000)
    assert session_api.load_learned(path) == 0.425
    assert session_api.effective_threshold(0.7, 0.425) == 0.425
    assert session_api.effective_threshold(0.3, 0.425) == 0.3      # the user's lower value wins
    assert session_api.effective_threshold(0, 0.425) == 0          # never stays never
    path.write_text("not json", encoding="utf-8")
    assert session_api.load_learned(path) is None                  # a bad file is no file
