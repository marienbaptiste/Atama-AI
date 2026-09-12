"""Cross-session memory (spec §6b, ADR-031): turn log, prompt section, recent topics, summariser.

Hermetic: no brain, no network. The summariser is driven by a fake `ask`.
"""
from __future__ import annotations

import asyncio
import json
import os

from backend import memory as memory_api
from backend import prompt
from backend.brain import BrainError, TextDelta, TurnComplete, reply_text
from backend.memory import Memory, excerpt_of, parse_summary


def mem(tmp_path, session="s1") -> Memory:
    return Memory(root=tmp_path / "memory", sessions=tmp_path / "sessions", session_id=session)


def rows(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# --------------------------------------------------------------------- turn log
def test_turn_record_carries_every_schema_key_even_when_unknown(tmp_path):
    """The log is the Anki mine: a consumer must never have to guess what a missing key means."""
    m = mem(tmp_path)
    m.record_turn(student="こんにちは", tutor_sentences=[{"text": "はい。", "emotion": "happy",
                                                          "synth_ms": None}])
    [row] = rows(m.log_path())
    assert set(row) == {"ts", "session", "persona", "turn", "student", "tutor", "tools",
                        "latency", "usage"}
    assert set(row["student"]) >= {"text", "audio_ms", "stt_ms"}
    assert set(row["latency"]) >= {"ttft_ms", "first_audio_ms", "voice_to_voice_ms"}
    assert row["tutor"]["text"] == "はい。" and row["session"] == "s1" and row["turn"] == 1


def test_turn_log_is_append_only(tmp_path):
    a = mem(tmp_path)
    a.record_turn(student="一", tutor_sentences=[])
    b = mem(tmp_path)                                   # a second writer, same session
    b.turn = 1
    b.record_turn(student="二", tutor_sentences=[])
    assert [r["student"]["text"] for r in rows(a.log_path())] == ["一", "二"]


def test_a_half_written_last_line_does_not_poison_the_log(tmp_path):
    """A crash mid-write leaves everything before it readable."""
    m = mem(tmp_path)
    m.record_turn(student="完全", tutor_sentences=[])
    with m.log_path().open("a", encoding="utf-8") as f:
        f.write('{"ts": "2026-09-10T00:00:00", "stud')      # the process died here
    assert excerpt_of(m.log_path()) == "STUDENT: 完全"


# ------------------------------------------------------------------ rendering
def test_no_memory_renders_to_nothing_not_to_empty_headings(tmp_path):
    """A first-ever session must read exactly as it did before memory existed."""
    assert mem(tmp_path).render() == ""


def test_render_includes_brief_topics_and_notes(tmp_path):
    m = mem(tmp_path)
    m.apply_summary("old", "2026-09-09", {"brief": "台風の話をした。", "topics": ["台風", "新幹線"],
                                          "notes": ["Works as an engineer."]})
    text = m.render()
    assert "LAST SESSION" in text and "台風の話をした。" in text
    assert "RECENTLY DISCUSSED" in text and "台風" in text and "新幹線" in text
    assert "ABOUT THE STUDENT" in text and "engineer" in text


def test_recent_topics_are_newest_first_and_deduplicated(tmp_path):
    m = mem(tmp_path)
    m.apply_summary("a", "2026-09-01", {"brief": "x", "topics": ["台風", "寿司"]})
    m.apply_summary("b", "2026-09-02", {"brief": "y", "topics": ["新幹線", "台風"]})
    assert m.recent_topics() == ["新幹線", "台風", "寿司"]


def test_only_the_last_few_sessions_are_shown(tmp_path):
    """Topics must stay one short line of prompt, however long the history grows."""
    m = mem(tmp_path)
    for i in range(memory_api.RECENT_SESSIONS + 5):
        m.apply_summary(f"s{i}", "2026-09-01", {"brief": "x", "topics": [f"話題{i}"]})
    shown = m.recent_topics()
    assert len(shown) == memory_api.RECENT_SESSIONS
    assert "話題0" not in shown and f"話題{memory_api.RECENT_SESSIONS + 4}" in shown


def test_editor_comments_in_student_notes_never_reach_the_prompt(tmp_path):
    m = mem(tmp_path)
    m.apply_summary("a", "2026-09-01", {"brief": "x", "topics": [], "notes": ["Likes trains."]})
    assert "Edit freely" in m.student_md.read_text(encoding="utf-8")
    assert "Edit freely" not in m.render() and "Likes trains." in m.render()


def test_memory_can_never_be_committed(tmp_path):
    """It holds the student's life (spec §11). Outside the repository, therefore outside git —
    and no stray copy of one of its files inside the tree (user, 2026-09-12)."""
    from backend import config
    cfg = config.load(tmp_path / "settings.json", env={})
    root = memory_api.memory_dir(cfg)
    assert config.REPO_ROOT not in root.parents and root != config.REPO_ROOT
    names = {"student.md", "about-me.md", "facts.md", "topics.jsonl", "last-session.md"}
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__"}     # not ours, and huge
    strays = []
    for here, dirs, files in os.walk(config.REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        strays += [os.path.join(here, f) for f in files if f in names]
    assert strays == [], f"memory files inside the repo: {strays}"


# ------------------------------------------------------ what the two of them know (user request)
def test_both_sides_of_the_memory_reach_the_prompt(tmp_path):
    """A tutor the student has met before: their name and life, and the tutor's own claims."""
    m = mem(tmp_path)
    m.apply_summary("a", "2026-09-01", {"brief": "x", "topics": ["猫"],
                                        "student_facts": ["Called Baptiste", "Lives in Belgium"],
                                        "tutor_facts": ["Has a cat called モチ"]})
    text = m.render()
    assert "WHAT YOU TWO ALREADY KNOW" in text
    assert "Baptiste" in text and "Belgium" in text and "モチ" in text
    assert m.facts() == (["Called Baptiste", "Lives in Belgium"], ["Has a cat called モチ"])


def test_a_fact_is_learned_once_however_it_is_worded(tmp_path):
    m = mem(tmp_path)
    m.apply_summary("a", "2026-09-01", {"brief": "x", "topics": ["猫"],
                                        "student_facts": ["Lives in Belgium"]})
    m.apply_summary("b", "2026-09-02", {"brief": "y", "topics": ["猫"],
                                        "student_facts": ["lives in Belgium.", "Has a dog"]})
    assert m.facts()[0] == ["Lives in Belgium", "Has a dog"]


def test_the_first_things_learned_survive_a_full_list(tmp_path):
    """The name is learned in lesson one and must not be pushed out by a month of small talk —
    while the newest few always get a slot, or the memory freezes the day it fills up."""
    m = mem(tmp_path)
    m.apply_summary("a", "2026-09-01", {"brief": "x", "topics": ["猫"],
                                        "student_facts": ["Called Baptiste"]})
    for i in range(memory_api.STUDENT_FACTS + 4):
        m.apply_summary(f"s{i}", "2026-09-02", {"brief": "x", "topics": ["猫"],
                                                "student_facts": [f"Ate cake number {i}"]})
    shown = m.render()
    assert "Called Baptiste" in shown
    assert f"Ate cake number {memory_api.STUDENT_FACTS + 3}" in shown
    assert "Ate cake number 8" not in shown           # the middle is what gets dropped
    student, _ = m.facts()
    assert len(memory_api._keep(student, memory_api.STUDENT_FACTS)) == memory_api.STUDENT_FACTS


def test_the_facts_files_are_editable_and_their_comments_stay_out_of_the_prompt(tmp_path):
    m = mem(tmp_path)
    m.apply_summary("a", "2026-09-01", {"brief": "x", "topics": ["猫"],
                                        "student_facts": ["Called Baptiste"],
                                        "tutor_facts": ["Lives in Kanazawa"]})
    assert "Edit or delete any line" in m.about_md.read_text(encoding="utf-8")
    assert "Edit or delete any line" not in m.render()
    m.facts_md.write_text("# About the tutor\n", encoding="utf-8")   # deleted by hand
    assert m.facts() == (["Called Baptiste"], [])
    assert "Kanazawa" not in m.render() and "Baptiste" in m.render()


def test_a_summary_with_no_facts_leaves_the_ones_on_disk_alone(tmp_path):
    m = mem(tmp_path)
    m.apply_summary("a", "2026-09-01", {"brief": "x", "topics": ["猫"], "student_facts": ["Called Baptiste"]})
    m.apply_summary("b", "2026-09-02", {"brief": "y", "topics": ["寿司"]})
    assert m.facts()[0] == ["Called Baptiste"]


def test_each_tutor_has_their_own_memory_and_the_student_is_shared(tmp_path):
    """Switch tutor and you meet someone who was not there last Tuesday — but who still knows
    your name (user, 2026-09-12)."""
    shared = tmp_path / "memory"
    minami = Memory(root=shared / "minami", sessions=tmp_path / "sessions", persona="minami", shared=shared)
    minami.apply_summary("a", "2026-09-01", {"brief": "猫の話をした。", "topics": ["猫"],
                                             "student_facts": ["Called Baptiste"],
                                             "tutor_facts": ["Has a cat called モチ"]})
    mori = minami.for_persona("mori")
    assert mori.facts() == (["Called Baptiste"], [])      # the student, not the other tutor's cat
    assert "モチ" not in mori.render() and "Baptiste" in mori.render()
    assert "猫の話をした。" not in mori.render()             # nor the lesson they were not at
    assert "モチ" in minami.render()


def test_a_lesson_is_summarised_by_the_tutor_who_taught_it(tmp_path):
    shared, sessions = tmp_path / "memory", tmp_path / "sessions"
    minami = Memory(root=shared / "minami", sessions=sessions, session_id="now",
                    persona="minami", shared=shared)
    minami.session_id = "old"
    minami.record_turn(student="こんにちは", tutor_sentences=[{"text": "はい。"}])
    minami.session_id = "now"
    mori = minami.for_persona("mori")
    assert [p.name for p in minami.pending_logs()] == [minami.sessions.glob("*.jsonl").__next__().name]
    assert mori.pending_logs() == []


def test_a_memory_from_before_the_split_belongs_to_whoever_is_teaching_now(tmp_path):
    shared = tmp_path / "memory"
    shared.mkdir(parents=True)
    (shared / "last-session.md").write_text("(2026-09-01) 猫の話をした。\n", encoding="utf-8")
    (shared / "student.md").write_text("- Works as an engineer.\n", encoding="utf-8")
    m = Memory(root=shared / "minami", sessions=tmp_path / "sessions", persona="minami", shared=shared)
    m.migrate()
    assert (shared / "minami" / "last-session.md").exists()
    assert not (shared / "last-session.md").exists()
    assert (shared / "student.md").exists()               # the student stays everyone's
    assert "猫の話をした。" in m.render() and "engineer" in m.render()


def test_a_session_nobody_spoke_in_is_never_asked_about(tmp_path):
    """Three old launches where the tutor greeted an empty room were re-summarised at every start,
    each costing a model call and the first one the CLI's cold start (live, 2026-09-12)."""
    m = mem(tmp_path)
    m.session_id = "quiet"
    m.record_turn(student="", tutor_sentences=[{"text": "こんにちは。"}])
    m.session_id = "now"
    asked = []

    async def ask(prompt):
        asked.append(prompt)
        return '{"brief": "x", "topics": ["y"]}'

    assert asyncio.run(m.summarise_pending(ask, "sum")) == 0
    assert asked == []                       # not one call spent on it
    assert m.pending_logs() == []            # and it never comes back


def test_an_answer_of_nothing_is_not_asked_twice(tmp_path):
    """A lesson the summariser judges empty is done with; only a failed CALL stays pending."""
    m = mem(tmp_path)
    m.session_id = "short"
    m.record_turn(student="はい", tutor_sentences=[{"text": "こんにちは。"}])
    m.session_id = "now"
    calls = []

    async def empty(prompt):
        calls.append(1)
        return '{"brief": "", "topics": []}'

    assert asyncio.run(m.summarise_pending(empty, "sum")) == 0
    assert len(calls) == 1 and m.pending_logs() == []

    m2, broken = mem(tmp_path, session="other"), []

    async def fails(prompt):
        broken.append(1)
        raise RuntimeError("rate limited")

    m2.session_id = "again"
    m2.record_turn(student="はい", tutor_sentences=[{"text": "はい。"}])
    m2.session_id = "now"
    assert asyncio.run(m2.summarise_pending(fails, "sum")) == 0
    assert len(m2.pending_logs()) == 1       # a failed call is worth retrying, and stays


# ---------------------------------------------------------------- summarising
def test_a_malformed_summary_changes_nothing(tmp_path):
    """A failed summarise must leave yesterday's memory intact, not truncate it to nothing."""
    m = mem(tmp_path)
    m.apply_summary("a", "2026-09-01", {"brief": "良い授業だった。", "topics": ["寿司"]})
    before = m.brief_md.read_text(encoding="utf-8")
    assert m.apply_summary("b", "2026-09-02", {"brief": "", "topics": []}) is False
    assert m.brief_md.read_text(encoding="utf-8") == before
    assert m.recent_topics() == ["寿司"]


def test_notes_are_not_duplicated_across_sessions(tmp_path):
    m = mem(tmp_path)
    m.apply_summary("a", "2026-09-01", {"brief": "x", "topics": [], "notes": ["Likes trains."]})
    m.apply_summary("b", "2026-09-02", {"brief": "y", "topics": [], "notes": ["likes trains."]})
    assert m.student_md.read_text(encoding="utf-8").lower().count("likes trains.") == 1


def test_pending_logs_skip_summarised_sessions_and_the_live_one(tmp_path):
    past = mem(tmp_path, session="past")
    past.record_turn(student="昨日", tutor_sentences=[{"text": "はい。"}])
    done = mem(tmp_path, session="done")
    done.record_turn(student="前", tutor_sentences=[{"text": "はい。"}])
    done.apply_summary("done", done.date, {"brief": "x", "topics": ["a"]})   # the log's own date, as summarise_pending writes it
    live = mem(tmp_path, session="live")
    live.record_turn(student="今", tutor_sentences=[{"text": "はい。"}])
    assert [p.stem.split("-", 3)[-1] for p in live.pending_logs()] == ["past"]


def test_summarise_pending_lands_a_parsed_reply(tmp_path):
    past = mem(tmp_path, session="past")
    past.record_turn(student="台風が来ます", tutor_sentences=[{"text": "そうですね。"}])
    now = mem(tmp_path, session="now")
    seen = []

    async def ask(text):
        seen.append(text)
        return 'Sure! {"brief": "台風の話。", "topics": ["台風"], "notes": ["Lives in Yokohama."]}'

    landed = asyncio.run(now.summarise_pending(ask, "INSTRUCTIONS"))
    assert landed == 1
    assert "STUDENT: 台風が来ます" in seen[0] and seen[0].startswith("INSTRUCTIONS")
    assert now.recent_topics() == ["台風"] and "Yokohama" in now.render()
    assert now.pending_logs() == []                      # never summarised twice


def test_the_launch_summarises_one_session_and_leaves_the_rest_for_the_background(tmp_path):
    """2026-09-12: five pending sessions held the launch for 80 s and looked like a hang."""
    for session in ("aaa", "bbb", "ccc"):
        mem(tmp_path, session=session).record_turn(student="質問", tutor_sentences=[{"text": "はい。"}])
    now = mem(tmp_path, session="live")
    asked = []

    async def ask(prompt):
        asked.append(prompt)
        return '{"brief": "b", "topics": ["t"]}'

    assert asyncio.run(now.summarise_pending(ask, "I", limit=1)) == 1
    assert len(asked) == 1                                   # one model call, not three
    assert len(now.pending_logs()) == 2                      # the rest wait for the background
    assert asyncio.run(now.summarise_pending(ask, "I")) == 2  # which then catches them up
    assert now.pending_logs() == []


def test_a_background_catch_up_never_replaces_a_newer_brief(tmp_path):
    now = mem(tmp_path, session="live")
    now.apply_summary("today", "2026-09-12", {"brief": "the newest lesson", "topics": ["new"]})
    now.apply_summary("older", "2026-09-01", {"brief": "an old lesson", "topics": ["old"]})
    assert "the newest lesson" in now.brief_md.read_text(encoding="utf-8")
    assert set(now.recent_topics()) == {"new", "old"}        # both are still remembered


def test_a_summariser_that_fails_costs_recall_not_the_lesson(tmp_path):
    past = mem(tmp_path, session="past")
    past.record_turn(student="何か", tutor_sentences=[{"text": "はい。"}])
    now = mem(tmp_path, session="now")

    async def ask(text):
        raise RuntimeError("model unavailable")

    assert asyncio.run(now.summarise_pending(ask, "I")) == 0
    assert now.render() == "" and len(now.pending_logs()) == 1   # retried next launch


class _FakeBrain:
    """A Brain whose one turn replays the given events — the shape the summariser's `ask` sees."""

    name = "fake"

    def __init__(self, events):
        self.events = events

    async def turn(self, text):
        for ev in self.events:
            yield ev


def test_a_summariser_turn_that_errors_leaves_the_lesson_pending(tmp_path):
    """Through the real `ask` shape (repl._summarise uses brain.reply_text). The bug: `ask`
    collected only the text, so an API error came back as "" and the lesson was marked done."""
    past = mem(tmp_path, session="past")
    past.record_turn(student="何か", tutor_sentences=[{"text": "はい。"}])
    now = mem(tmp_path, session="now")
    failing = _FakeBrain([BrainError("API Error: 529 overloaded"), TurnComplete()])

    assert asyncio.run(now.summarise_pending(lambda t: reply_text(failing, t), "I")) == 0
    assert len(now.pending_logs()) == 1                      # still pending: retried next launch

    fine = _FakeBrain([TextDelta('{"brief": "b", "topics": ["t"]}'), TurnComplete()])
    assert asyncio.run(now.summarise_pending(lambda t: reply_text(fine, t), "I")) == 1
    assert now.pending_logs() == []


def test_a_lesson_is_one_file_whatever_the_clock_says(tmp_path):
    """Crossing midnight used to start a second file with the same session id."""
    m = mem(tmp_path)
    m.date = "2026-09-12"
    m.record_turn(student="一", tutor_sentences=[])
    m.record_turn(student="二", tutor_sentences=[])           # "the next day" changes nothing
    assert m.log_path().name == "2026-09-12-s1.jsonl" and len(rows(m.log_path())) == 2


def test_a_split_lesson_from_an_older_writer_is_summarised_on_both_days(tmp_path):
    """Two files, one session id, the first already summarised: the second must not read as done
    because the first is. The done-marker is keyed by (date, session) — the two things every
    topics row has always carried, so existing markers still count."""
    first = mem(tmp_path, session="night")
    first.date = "2026-09-11"
    first.record_turn(student="夜", tutor_sentences=[{"text": "はい。"}])
    second = mem(tmp_path, session="night")
    second.date = "2026-09-12"
    second.record_turn(student="朝", tutor_sentences=[{"text": "はい。"}])
    now = mem(tmp_path, session="now")
    now.apply_summary("night", "2026-09-11", {"brief": "x", "topics": ["夜"]})   # the old marker
    assert [p.name for p in now.pending_logs()] == ["2026-09-12-night.jsonl"]


def test_the_handoff_carries_only_this_launch(tmp_path):
    """A log holding an earlier lesson's turns and none from this launch yields NO handoff — the
    replacement must greet, not be told "do not greet again" over a lesson that ended hours ago.
    Turns from this launch yield exactly those (the repl passes launch_stamp() as `since`)."""
    m = mem(tmp_path)
    m.log_path().parent.mkdir(parents=True)
    old = {"ts": "2026-09-12T08:00:00+00:00", "session": "s1", "turn": 1,
           "student": {"text": "朝の話"}, "tutor": {"text": "はい。"}}
    m.log_path().write_text(json.dumps(old, ensure_ascii=False) + "\n", encoding="utf-8")
    since = "2026-09-12T18:30:00+00:00"
    assert excerpt_of(m.log_path(), since=since) == ""            # nothing since this launch
    assert "朝の話" in excerpt_of(m.log_path())                    # the summariser still sees it
    later = {**old, "ts": "2026-09-12T18:30:00+00:00", "turn": 2, "student": {"text": "夜の話"}}
    with m.log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(later, ensure_ascii=False) + "\n")
    assert excerpt_of(m.log_path(), since=since) == "STUDENT: 夜の話\nTUTOR: はい。"
    assert prompt.build("x", handoff=excerpt_of(m.log_path(), since=since)).text.count("EARLIER IN THIS LESSON") == 1
    assert "EARLIER IN THIS LESSON" not in prompt.build("x", handoff="").text
    stamp = memory_api.launch_stamp()                               # same shape as a record's ts,
    assert len(stamp) == len(since) and stamp.endswith("+00:00")    # so `<` compares moments


def test_the_memory_headings_come_from_a_file_not_from_python(tmp_path):
    """ADR-012: what the model reads lives under prompts/."""
    say = memory_api.memory_headings()
    assert say["brief"] == "LAST SESSION" and say["topics"].startswith("RECENTLY DISCUSSED")
    assert say["known"].startswith("WHAT YOU TWO ALREADY KNOW")
    assert say["no_such_key"] == "NO_SUCH_KEY"               # a missing block is visible, not blank
    custom = tmp_path / "memory.md"
    custom.write_text("<!-- comment -->\n## brief\n前回\n## notes\n学生について\n", encoding="utf-8")
    assert memory_api.memory_headings(custom)["brief"] == "前回"


def test_parse_summary_digs_json_out_of_prose():
    assert parse_summary('ok {"brief": "a", "topics": []} done') == {"brief": "a", "topics": []}
    assert parse_summary("no json here") is None
    assert parse_summary("{broken") is None


# ---------------------------------------------------------------- the prompt
def test_memory_section_is_budgeted_and_reported(tmp_path):
    nl = chr(10)
    huge = nl.join(["覚えていること: " + "漢字" * 40] * 60)
    p = prompt.build("WaniKani level 4.", memory=huge)
    assert "memory" in p.truncated
    assert p.sections["memory"] <= prompt.MEMORY_MAX_TOKENS


def test_absent_memory_leaves_no_placeholder_behind():
    p = prompt.build("WaniKani level 4.")
    assert "{{memory}}" not in p.text and "RECENTLY DISCUSSED" not in p.text


def test_worst_case_with_memory_still_fits_the_total():
    """Soul, profile AND memory all maxed must fit — the real ceiling now has three sections."""
    nl = chr(10)
    p = prompt.build(nl.join(["語彙: " + "漢字" * 40] * 40),
                     soul=nl.join(["あいうえおかきくけこ" * 5] * 40),
                     memory=nl.join(["覚えていること: " + "漢字" * 40] * 60))
    assert {"soul", "student_profile", "memory"} <= set(p.truncated)
    assert p.tokens <= prompt.TOTAL_MAX_TOKENS, (
        f"template {p.tokens - sum(p.sections.values())} tokens; trim it or raise the total")
