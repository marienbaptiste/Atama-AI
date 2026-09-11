"""Cross-session memory (spec §6b, ADR-031): turn log, prompt section, recent topics, summariser.

Hermetic: no brain, no network. The summariser is driven by a fake `ask`.
"""
from __future__ import annotations

import asyncio
import json

from backend import memory as memory_api
from backend import prompt
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
    assert set(row) == {"ts", "session", "turn", "student", "tutor", "tools", "latency", "usage"}
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
    done.apply_summary("done", "2026-09-01", {"brief": "x", "topics": ["a"]})
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
