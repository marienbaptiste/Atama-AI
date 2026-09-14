"""The session summary, fixed 2026-09-14 (user): written when the lesson ends rather than held over
the next greeting; given a timeout of its own, because the tutor's 60 s cut every real lesson off
mid-thought and left it to fail again at every launch; and `progressed` counted by the study plan,
never copied by the model. Plus the gauges that waited for the page to be clicked."""
from __future__ import annotations

import asyncio
import json
import types

from backend import config, orchestrator, study_plan
from backend.brain import BrainError, TextDelta, TurnComplete
from backend.memory import Memory
from backend.tests.test_brain_claude_cli import FakeCli, cfg as cli_cfg, collect


def lesson_log(mem: Memory) -> None:
    mem.record_turn(student="", tutor_sentences=[{"text": "何を食べてみたいですか。", "target": "〜てみる"}])
    mem.record_turn(student="すしを食べてみたいです", tutor_sentences=[{"text": "いいですね。", "used": "〜てみる"}])
    mem.record_turn(student="天ぷらも食べてみます", tutor_sentences=[{"text": "すごい。", "used": "〜てみる"}])


def rows(mem: Memory) -> list[dict]:
    return [json.loads(l) for l in mem.topics_jsonl.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------- progressed is counted, not copied
def test_progressed_is_the_study_plans_count_whatever_the_model_says(tmp_path):
    past = Memory(root=tmp_path / "memory", sessions=tmp_path / "sessions", session_id="past")
    lesson_log(past)
    assert study_plan.progressed_in(past.log_path(), 2) == ["〜てみる"]
    assert study_plan.progressed_in(past.log_path(), 3) == []

    async def ask(text):   # a model that invents one and misses the real one
        return json.dumps({"brief": "食べ物の話。", "topics": ["すし"], "progressed": ["嘘"]}, ensure_ascii=False)

    now = Memory(root=tmp_path / "memory", sessions=tmp_path / "sessions", session_id="now")
    landed = asyncio.run(now.summarise_pending(ask, "I", progressed=lambda log: study_plan.progressed_in(log, 2)))
    assert landed == 1 and rows(now)[0]["progressed"] == ["〜てみる"]


def test_the_instructions_no_longer_ask_the_model_for_progressed():
    text = (config.REPO_ROOT / "prompts" / "summarise.md").read_text(encoding="utf-8")
    assert '"progressed"' not in text
    assert "e-mail" in text                      # the name comes from the transcript, never the account


# ------------------------------------------------------------- the lesson that is ending
def test_the_ending_lesson_is_summarised_though_pending_logs_leaves_it_out(tmp_path):
    mem = Memory(root=tmp_path / "memory", sessions=tmp_path / "sessions", session_id="today")
    lesson_log(mem)
    assert mem.pending_logs() == []              # still this session's: the launch path never sees it
    asked = []

    async def ask(text):
        asked.append(text)
        return json.dumps({"brief": "食べ物。", "topics": ["すし"]}, ensure_ascii=False)

    assert asyncio.run(mem.summarise_pending(ask, "I", logs=[mem.log_path()])) == 1
    assert len(asked) == 1 and rows(mem)[0]["topics"] == ["すし"]


def _lesson(tmp_path, mem, calls):
    lesson = orchestrator.Lesson.__new__(orchestrator.Lesson)
    lesson.cfg = types.SimpleNamespace(MEMORY_SUMMARY_MODEL="sonnet")
    lesson.mem = mem
    return lesson


def test_close_writes_the_lesson_down_and_says_so(tmp_path, monkeypatch, capsys):
    mem = Memory(root=tmp_path / "memory", sessions=tmp_path / "sessions", session_id="today")
    lesson_log(mem)
    calls = []

    async def fake_summarise(cfg, m, limit=None, logs=None):
        calls.append(logs)
        m.apply_summary("today", m.date, {"brief": "食べ物。", "topics": ["すし"]})
        return 1

    monkeypatch.setattr(orchestrator, "summarise", fake_summarise)
    asyncio.run(_lesson(tmp_path, mem, calls).remember_this_lesson())
    assert calls == [[mem.log_path()]]
    out = capsys.readouterr().out
    assert "writing down today's lesson" in out and "Ctrl+C skips it" in out and "memory: written" in out
    # The next launch checks first (user, 2026-09-14): written at shutdown, it is not pending, so
    # the launch makes no call and her greeting waits for nothing.
    later = Memory(root=tmp_path / "memory", sessions=tmp_path / "sessions", session_id="tomorrow")
    assert later.pending_logs() == []


def test_a_launch_where_nobody_spoke_costs_no_call_at_shutdown(tmp_path, monkeypatch, capsys):
    mem = Memory(root=tmp_path / "memory", sessions=tmp_path / "sessions", session_id="today")
    mem.record_turn(student="", tutor_sentences=[{"text": "こんにちは。"}])   # her greeting, nobody answered
    called = []

    async def fake_summarise(*a, **k):
        called.append(1)
        return 0

    monkeypatch.setattr(orchestrator, "summarise", fake_summarise)
    asyncio.run(_lesson(tmp_path, mem, []).remember_this_lesson())
    assert called == []
    assert "nothing to write down - you didn't speak this lesson" in capsys.readouterr().out


def test_a_failed_shutdown_summary_leaves_it_for_the_next_launch(tmp_path, monkeypatch, capsys):
    mem = Memory(root=tmp_path / "memory", sessions=tmp_path / "sessions", session_id="today")
    lesson_log(mem)

    async def failing(*a, **k):
        return 0                                    # summarise() never raises; a failure lands nothing

    monkeypatch.setattr(orchestrator, "summarise", failing)
    asyncio.run(_lesson(tmp_path, mem, []).remember_this_lesson())
    assert "the next launch will try again" in capsys.readouterr().out
    later = Memory(root=tmp_path / "memory", sessions=tmp_path / "sessions", session_id="tomorrow")
    assert later.pending_logs() == [mem.log_path()]


# ------------------------------------------------------------- the summariser's own timeout
def test_the_turn_timeout_can_be_longer_than_the_tutors(tmp_path):
    async def go():
        b = FakeCli(cli_cfg(tmp_path, CLAUDE_TURN_TIMEOUT_S="1"), turn_timeout_s=5)
        b.scenario = "late_result"                  # answers at 2 s: past the tutor's 1 s, inside 5 s
        await b.start()
        events = await collect(b)
        await b.aclose()
        return events

    events = asyncio.run(go())
    assert not any(isinstance(e, BrainError) for e in events)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "遅い答え"


def test_the_turn_timeout_can_be_shorter_than_the_tutors(tmp_path):
    async def go():
        b = FakeCli(cli_cfg(tmp_path, CLAUDE_TURN_TIMEOUT_S="60"), turn_timeout_s=1)
        b.scenario = "hang"
        await b.start()
        events = await collect(b)
        await b.aclose()
        return events

    events = asyncio.run(go())
    assert any(isinstance(e, BrainError) and e.message == "turn exceeded 1s" for e in events)


def test_the_summary_timeout_is_a_setting_with_a_floor():
    s = {x.key: x for x in config.SCHEMA}["MEMORY_SUMMARY_TIMEOUT_S"]
    assert s.default == 240 and s.low == 30


# ------------------------------------------------------------- the gauges do not wait for a click
class Brain:
    async def turn(self, text):
        yield TextDelta("はい。")
        yield TurnComplete(text="はい。")


class SlowVoice:
    """Stands in for a page that has not been clicked: her audio does not finish."""

    def __init__(self):
        self.release = asyncio.Event()

    async def say(self, chunk):
        pass

    async def drain(self):
        await self.release.wait()


def test_the_opening_numbers_arrive_before_her_voice_has_played():
    async def go():
        voice, seen = SlowVoice(), []

        async def answered():
            seen.append("gauges")

        turn = asyncio.create_task(orchestrator.one_turn(Brain(), "hi", voice, opening=True, answered=answered))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if seen:
                break
        before_release, still_playing = list(seen), not turn.done()
        voice.release.set()
        await turn
        return before_release, still_playing

    before_release, still_playing = asyncio.run(go())
    assert still_playing and before_release == ["gauges"]    # in while her audio was still unplayed


def test_the_gpu_gauge_gets_a_reading_at_launch(monkeypatch):
    sent = []

    class Hub:
        async def meters(self, **fields):
            sent.append(fields)

    monkeypatch.setattr(orchestrator.vram_mod, "read",
                        lambda: types.SimpleNamespace(used_mib=2048, total_mib=16384))
    lesson = orchestrator.Lesson.__new__(orchestrator.Lesson)
    lesson.hub = Hub()
    asyncio.run(lesson._first_vram())
    assert sent == [{"vram_used_mib": 2048, "vram_total_mib": 16384}]


# ------------------------------------------------------------- the exit, in order (user, 2026-09-14)
def test_shutdown_takes_everything_down_first_and_writes_the_memory_last(monkeypatch):
    order = []

    class Explainer:
        async def aclose(self):
            order.append("explainer")

    async def catchup():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            order.append("catch-up closed")           # its claude process is closed before anything else
            raise

    async def containers(hub):
        order.append("containers")

    async def remember(self):
        order.append("memory")

    async def go():
        lesson = orchestrator.Lesson.__new__(orchestrator.Lesson)
        lesson.catchup, lesson.summary = asyncio.create_task(catchup()), None
        await asyncio.sleep(0)
        lesson.explainer, lesson.voice, lesson.brain, lesson.server_task, lesson.hub = Explainer(), None, None, None, object()
        await lesson.close()

    monkeypatch.setattr(orchestrator, "stop_containers", containers)
    monkeypatch.setattr(orchestrator.Lesson, "remember_this_lesson", remember)
    asyncio.run(go())
    assert order == ["catch-up closed", "explainer", "containers", "memory"]


def test_a_stop_that_keeps_the_containers_says_so(capsys):
    asyncio.run(orchestrator.stop_containers(types.SimpleNamespace(quit_requested=False)))
    assert "containers stay up" in capsys.readouterr().out
    asyncio.run(orchestrator.stop_containers(None))    # no page at all: nothing to say
    assert capsys.readouterr().out == ""


def test_the_background_catch_up_reports_once_when_done(capsys):
    async def go():
        async def three():
            return 3
        task = asyncio.create_task(three())
        await task
        orchestrator._caught_up(task)
    asyncio.run(go())
    out = capsys.readouterr().out
    assert "caught up on 3 earlier lessons" in out and "summarising" not in out
