"""Today's targets (spec §6c, ADR-038): the ledger folded from turn logs, the ranking, the SRS
spacing, mid-session progression, the opener rotation, the prompt block and the coach note —
and that the note reaches the brain only. Hermetic: synthetic records, a fake brain, no files
outside tmp_path, zero model calls by construction."""
from __future__ import annotations

import asyncio
import json

import numpy as np

from backend import config, orchestrator, prompt, study_plan
from backend.brain import TextDelta, TurnComplete
from backend.memory import Memory
from backend.stt import Transcript
from backend.study import Item
from backend.voice_loop import VoiceLoop

V = [Item("貯金", "vocab", "ちょきん", "savings, to save money", 2),
     Item("公園", "vocab", "こうえん", "park", 1),
     Item("勉強", "vocab", "べんきょう", "study", 3),
     Item("難しい", "vocab", "むずかしい", "difficult", 3, leech=True),
     Item("図書館", "vocab", "としょかん", "library", 1),
     Item("新幹線", "vocab", "しんかんせん", "bullet train", 2),
     Item("大切", "vocab", "たいせつ", "important", 4),
     Item("約束", "vocab", "やくそく", "promise, appointment", 2),
     Item("天気", "vocab", "てんき", "weather", 4),
     Item("野菜", "vocab", "やさい", "vegetables", 1)]
G = [Item("〜たら", "grammar", srs="ghost"), Item("〜てみる", "grammar", srs="beginner"),
     Item("〜ておく", "grammar", srs="adept"), Item("〜ながら", "grammar", srs="seasoned"),
     Item("〜てしまう", "grammar", srs="beginner"), Item("〜ことがある", "grammar", srs="ghost")]
ITEMS = V + G


def turn(session, n, student="", tutor=(), used="", target="", grammar=(), date="2026-09-01"):
    """One §6b turn record. `tutor` are her sentences; the marks ride on the first one."""
    sentences = [{"text": t, "emotion": None, "synth_ms": None} for t in tutor]
    if sentences:
        if used:
            sentences[0]["used"] = used
        if target:
            sentences[0]["target"] = target
        if grammar:
            sentences[0]["grammar"] = [{"span": g, "point": g} for g in grammar]
    return {"ts": f"{date}T10:{n:02d}:00+00:00", "session": session, "persona": "minami", "turn": n,
            "student": {"text": student, "audio_ms": None, "stt_ms": None},
            "tutor": {"text": "".join(tutor), "sentences": sentences}, "tools": [], "latency": {}, "usage": {}}


def plan_for(ledger=None, items=ITEMS, **kw):
    return study_plan.select(items, ledger or study_plan.fold([], items), **kw)


def keys(targets):
    return [t.item.text for t in targets]


# ------------------------------------------------------------------------ the fold
def test_the_ledger_is_folded_from_the_marks_and_the_student_transcript():
    records = [turn("s1", 1, student="公園で勉強します", tutor=["雨が降ったら公園に行きます。"], grammar=["〜たら"],
                    target="〜てみる"),
                turn("s1", 2, student="貯金してみます", tutor=["いいですね。"], used="〜てみる")]
    led = study_plan.fold(records, ITEMS)
    assert led.today == 1
    assert led.get("公園").heard == 1 and led.get("公園").attempted == 1 and led.get("公園").last_seen == "2026-09-01"
    assert led.get("たら").heard == 1 and led.get("てみる").heard == 1
    assert led.get("てみる").produced == 1 and led.get("てみる").last_produced == "2026-09-01"
    assert led.get("勉強").attempted == 1 and led.get("勉強").heard == 0       # the student's, not hers
    assert led.get("貯金").attempted == 1


def test_a_success_round_doubles_the_gap_and_a_lapse_resets_it():
    def session(idx, produced, attempted):
        rows = [turn(f"s{idx}", 1, tutor=["貯金は大切です。"], target="貯金", date=f"2026-09-{idx + 1:02d}")]
        for n in range(produced):
            rows.append(turn(f"s{idx}", 2 + n, student="貯金します", tutor=["はい。"], used="貯金",
                             date=f"2026-09-{idx + 1:02d}"))
        if attempted and not produced:
            rows.append(turn(f"s{idx}", 9, student="貯金", tutor=["違います。"], date=f"2026-09-{idx + 1:02d}"))
        return rows

    records = session(0, 2, True) + session(1, 2, True) + session(2, 2, True)
    led = study_plan.fold(records, ITEMS)
    e = led.get("貯金")
    assert e.streak == 3 and e.lapses == 0 and e.progressed == "2026-09-03"
    assert e.due_session == 2 + 4                       # after rounds 1, 2, 3 the gaps were 1, 2, 4
    assert [study_plan.gap_after(s, 1, 32) for s in (1, 2, 3, 4, 5, 6, 7)] == [1, 2, 4, 8, 16, 32, 32]
    led = study_plan.fold(records + session(6, 0, True), ITEMS)       # the 4th session: index 3
    e = led.get("貯金")
    assert e.streak == 0 and e.lapses == 1 and e.due_session == 3 + 1   # back next session
    # heard, never attempted: unchanged, still due at what it was
    led2 = study_plan.fold(session(0, 2, True) + session(1, 0, False), ITEMS)
    assert led2.get("貯金").streak == 1 and led2.get("貯金").due_session == 1 and led2.get("貯金").lapses == 0


def test_the_fold_reads_every_tutors_logs_and_caps_the_sessions():
    records = [turn(f"s{i}", 1, tutor=["公園に行きます。"], date=f"2026-08-{i + 1:02d}") for i in range(5)]
    records[2]["persona"] = "tanaka"
    led = study_plan.fold(records, ITEMS, sessions=3)
    assert led.today == 3 and led.get("公園").heard == 3


# ---------------------------------------------------------------------- selection
def test_ranking_is_weakness_then_never_covered_then_most_overdue_then_ties_on_text():
    plan = plan_for(target_vocab=4, target_grammar=6)
    assert keys(plan.vocab)[0] == "難しい"                     # the leech first
    assert keys(plan.vocab)[1:] == ["公園", "図書館", "野菜"]     # stage 1 next, ties alphabetical
    assert keys(plan.grammar)[:2] == ["〜ことがある", "〜たら"]    # ghosts, ties on text
    assert keys(plan.grammar)[2:4] == ["〜てしまう", "〜てみる"]  # beginner before adept before seasoned
    # covered items fall behind never-covered ones of the same weakness
    led = study_plan.fold([turn("s1", 1, tutor=["公園に行きます。"])], ITEMS)
    plan = plan_for(led, target_vocab=4)
    assert keys(plan.vocab) == ["難しい", "図書館", "野菜", "公園"]
    # and among covered ones, the most overdue (smallest due) comes first, then more lapses
    led = study_plan.fold([], ITEMS)
    led.entry("図書館").heard, led.entry("図書館").due_session = 1, 0
    led.entry("野菜").heard, led.entry("野菜").due_session = 1, 0
    led.entry("野菜").lapses = 2
    led.entry("公園").heard, led.entry("公園").due_session = 1, 0
    led.entry("公園").produced = 3
    led.today = 5
    assert keys(plan_for(led, target_vocab=4).vocab) == ["難しい", "野菜", "図書館", "公園"]


def test_a_target_that_progressed_this_week_goes_to_the_back_whatever_its_weakness():
    """Or the weakest few would come straight back at a one-session gap and the rest of the list
    would never get a turn — coverage is the point (user, 2026-09-12)."""
    led = study_plan.fold([], ITEMS, date="2026-09-12")
    led.entry("難しい").progressed, led.entry("難しい").heard, led.entry("難しい").streak = "2026-09-10", 2, 1
    led.entry("公園").progressed, led.entry("公園").heard, led.entry("公園").streak = "2026-09-01", 2, 1
    plan = plan_for(led, target_vocab=3)
    assert keys(plan.vocab) == ["図書館", "野菜", "公園"] and plan.queue["vocab"][-1].text == "難しい"
    assert study_plan.recently_progressed(led.get("難しい"), "2026-09-17") is False   # a week later: back in play


def test_a_target_not_yet_due_is_left_out_while_due_ones_exist_and_fills_when_none_are():
    led = study_plan.fold([], ITEMS)
    led.today = 3
    led.entry("難しい").streak, led.entry("難しい").heard, led.entry("難しい").due_session = 2, 2, 7
    plan = plan_for(led, target_vocab=3)
    assert "難しい" not in keys(plan.vocab) and plan.queue["vocab"][-1].text == "難しい"
    for item in V:
        e = led.entry(study_plan.key_of(item))
        e.heard, e.due_session = 1, 10
    led.entry("貯金").due_session = 4
    assert keys(plan_for(led, target_vocab=2).vocab) == ["貯金", "難しい"]   # soonest-due fill


def test_round_robin_visits_every_item_across_sessions():
    records: list[dict] = []
    seen: set[str] = set()
    for s in range(5):
        date = f"2026-09-{s + 1:02d}"
        led = study_plan.fold(records, ITEMS, date=date)              # daily lessons
        plan = plan_for(led, target_vocab=4, target_grammar=2)
        seen |= set(keys(plan.targets))
        for n, t in enumerate(plan.targets, start=1):     # she uses each; the student produces each twice
            records.append(turn(f"s{s}", n, tutor=[t.item.text + "。"], target=t.item.text, date=date))
            records.append(turn(f"s{s}", 20 + n, student=t.item.text, tutor=["はい。"], used=t.item.text, date=date))
            records.append(turn(f"s{s}", 40 + n, student=t.item.text, tutor=["はい。"], used=t.item.text, date=date))
    assert seen == {i.text for i in ITEMS}


def test_a_well_handled_item_returns_less_and_less_while_a_lapsing_one_keeps_coming_back():
    import datetime as dt
    good, bad = Item("良い", "vocab", "よい", "good", 1), Item("悪い", "vocab", "わるい", "bad", 1)
    items = [good, bad]
    records: list[dict] = []
    selected_good, selected_bad = [], []
    for s in range(10):
        date = (dt.date(2026, 9, 1) + dt.timedelta(days=8 * s)).isoformat()   # weekly-ish lessons
        led = study_plan.fold(records, items, date=date)
        plan = plan_for(led, items=items, target_vocab=1, target_grammar=0)
        chosen = keys(plan.vocab)[0]
        (selected_good if chosen == "良い" else selected_bad).append(s)
        records.append(turn(f"s{s}", 1, tutor=[chosen + "。"], target=chosen, date=date))
        if chosen == "良い":                                   # produced twice: a success round
            records += [turn(f"s{s}", 2, student="良い", tutor=["。"], used="良い", date=date),
                        turn(f"s{s}", 3, student="良い", tutor=["。"], used="良い", date=date)]
        else:                                                  # attempted, never credited: a lapse
            records.append(turn(f"s{s}", 2, student="悪い", tutor=["違います。"], date=date))
    # 悪い sorts first on text. Its lapses bring it back the next session every time; 良い's due
    # doubles after each round (due 2, then 5, then 10) and it is seen at widening gaps — the
    # ordering between two due items prefers the more overdue and the more lapsed one.
    assert selected_bad == [0, 2, 4, 5, 7, 8, 9] and selected_good == [1, 3, 6]
    led = study_plan.fold(records, items, date="2026-12-01")
    assert led.get("悪い").lapses == 7 and led.get("悪い").streak == 0
    assert led.get("良い").streak == 3 and led.get("良い").due_session == 6 + 4 and led.get("良い").lapses == 0


# -------------------------------------------------------------------- progression
def test_two_correct_productions_retire_a_target_and_promote_the_next_of_its_kind():
    plan = plan_for(target_vocab=2, target_grammar=1)
    assert keys(plan.vocab) == ["難しい", "公園"] and keys(plan.grammar) == ["〜ことがある"]
    assert not plan.note_turn(turn("now", 1, student="難しいです", tutor=["そうですね。"], used="難しい"))
    change = plan.note_turn(turn("now", 2, student="難しい", tutor=["はい。"], used="難しい"))
    assert [(i.text, gap) for i, gap in change.retired] == [("難しい", 1)]
    assert [i.text for i in change.promoted] == ["図書館"]
    assert keys(plan.vocab) == ["公園", "図書館"] and keys(plan.grammar) == ["〜ことがある"]
    e = plan.ledger.get("難しい")
    assert e.streak == 1 and e.due_session == plan.ledger.today + 1 and e.progressed == plan.date
    # a second progression later in the session cannot bring it back
    plan2 = plan.reselect(ITEMS)
    assert "難しい" not in keys(plan2.vocab) and keys(plan2.vocab) == ["公園", "図書館"]
    assert plan2.turns == 2 and plan2.retired == plan.retired
    # the gap grows with the streak, and the terminal wording is what the coach note says
    plan.ledger.entry("公園").streak = 2
    change = plan.note_turn(turn("now", 3, student="公園", tutor=["。"], used="公園"))
    change = plan.note_turn(turn("now", 4, student="公園", tutor=["。"], used="公園"))
    assert [(i.text, gap) for i, gap in change.retired] == [("公園", 4)]


def test_a_grammar_progression_promotes_grammar_not_vocab():
    plan = plan_for(target_vocab=1, target_grammar=1)
    for n in (1, 2):
        change = plan.note_turn(turn("now", n, student="行ったことがある", tutor=["。"], used="〜ことがある"))
    assert [i.text for i in change.promoted] == ["〜たら"] and keys(plan.vocab) == ["難しい"]


# ------------------------------------------------------------------- the coach note
def test_the_coach_note_keeps_its_cadence_and_says_what_changed():
    plan = plan_for(target_vocab=3, target_grammar=1, nudge_every=3)
    assert study_plan.coach_note(plan, 0) == "" and study_plan.coach_note(plan, 1) == ""
    note = study_plan.coach_note(plan, 3)
    assert note.startswith("[coach:") and note.endswith("]") and "not yet used: 難しい、公園、図書館" in note
    assert prompt.estimate_tokens(note) <= study_plan.NOTE_MAX_TOKENS
    plan.note_turn(turn("now", 1, tutor=["難しいですね。"], target="〜ことがある"))     # she used two
    note = study_plan.coach_note(plan, 3)
    assert "not yet used: 公園、図書館" in note and "elicit 難しい next" in note
    for n in (2, 3):
        plan.note_turn(turn("now", n, student="難しい", tutor=["。"], used="難しい"))
    note = study_plan.coach_note(plan, plan.turns)                       # the turn after a progression
    assert "難しい progressed (back after 1 sessions) → new target 野菜" in note
    assert study_plan.coach_note(plan, plan.turns + 1) == ""             # and not the one after that
    assert study_plan.coach_note(plan_for(nudge_every=0), 3) == ""       # 0 = off


def test_the_coach_note_never_exceeds_its_budget():
    long = [Item("超長い単語" * 3, "vocab", "x", "y", 1) for _ in range(1)] + \
           [Item(f"言葉{n}" * 4, "vocab", "x", "y", 1) for n in range(6)]
    plan = plan_for(items=long, target_vocab=6, target_grammar=0, nudge_every=1)
    note = study_plan.coach_note(plan, 1)
    assert note and prompt.estimate_tokens(note) <= study_plan.NOTE_MAX_TOKENS


# ------------------------------------------------------------------------ the opener
def test_the_opener_cycles_and_scenario_and_personal_pick_deterministically():
    scenarios = ["レストランで注文する", "駅で道を聞く", "病院の受付", "忘れ物を届ける"]
    kinds = [study_plan.opener_for(i, scenarios=scenarios, facts=["Lives in Kanazawa"]).kind for i in range(8)]
    assert kinds == ["news", "scenario", "personal", "story"] * 2
    assert study_plan.opener_for(2, scenarios=scenarios, facts=[]).kind == "scenario"     # no fact: scenario
    assert study_plan.opener_for(1, scenarios=[], facts=[]).kind == "news"                # no list: news
    picked = study_plan.opener_for(1, scenarios=scenarios, recent_topics=["駅"]).subject
    assert picked in scenarios and "駅" not in picked
    assert study_plan.opener_for(1, scenarios=scenarios) == study_plan.opener_for(1, scenarios=scenarios)
    assert study_plan.opener_for(5, scenarios=scenarios).subject != study_plan.opener_for(1, scenarios=scenarios).subject
    story = study_plan.opener_for(3, targets=ITEMS)
    assert story.kind == "story" and story.subject == "貯金、公園、〜たら"
    assert len(study_plan.load_scenarios()) >= 20               # the shipped list


# ---------------------------------------------------------------------- the prompt
def test_the_block_fits_its_budget_and_carries_into_every_session():
    plan = plan_for(target_vocab=8, target_grammar=4)
    plan.opener = study_plan.Opener("scenario", "駅で道を聞く")
    text = study_plan.render(plan)
    assert prompt.estimate_tokens(text) <= study_plan.PLAN_MAX_TOKENS
    assert "TODAY'S TARGETS" in text and "貯金（ちょきん）savings" in text and "〜たら" in text
    assert "OPENER" in text and "駅で道を聞く" in text and "no search" in text
    p = prompt.build("WaniKani level 4.", study_plan=text, handoff="STUDENT: はい\nTUTOR: いいですね。")
    assert "駅で道を聞く" in p.text and p.sections["study_plan"] == prompt.estimate_tokens(text)
    assert p.text.index("STUDENT PROFILE") < p.text.index("TODAY'S TARGETS") < p.text.index("HARD OUTPUT RULES")
    assert p.truncated == [] and p.tokens <= prompt.TOTAL_MAX_TOKENS
    assert "{{study_plan}}" not in prompt.build("x").text          # absent: no placeholder left behind
    wide = [Item("非常に長い語彙" + str(n), "vocab", "ひじょうにながいごい", "a very long meaning indeed, twice", 1)
            for n in range(12)]
    assert prompt.estimate_tokens(study_plan.render(plan_for(items=wide, target_vocab=12))) <= study_plan.PLAN_MAX_TOKENS


def test_the_wording_lives_in_prompts_not_in_python():
    say = study_plan.wording()
    assert set(say) >= {"heading", "vocab", "grammar", "opener", "opener_news", "opener_scenario",
                        "opener_personal", "opener_story", "rule", "note", "pending", "elicit", "progressed"}
    assert "{{body}}" in say["note"] and "{{n}}" in say["progressed"]


# ----------------------------------------------------------- the note reaches the brain only
class FakeBrain:
    name = "fake"

    def __init__(self, reply="[used:難しい]はい。そうですね。"):
        self.reply = reply
        self.turns: list[str] = []

    async def turn(self, text):
        self.turns.append(text)
        for piece in self.reply:
            yield TextDelta(piece)
        yield TurnComplete(text=self.reply)


def test_the_note_goes_to_the_brain_and_never_into_the_turn_log_in_text_mode(tmp_path):
    plan = plan_for(target_vocab=2, target_grammar=1, nudge_every=1)
    brain = FakeBrain()
    mem = Memory(root=tmp_path / "memory", sessions=tmp_path / "sessions", session_id="s1")
    noted = []

    def note(student, sentences):
        noted.append(student)
        plan.note_turn({"student": {"text": student}, "tutor": {"sentences": sentences}})

    asyncio.run(orchestrator.one_turn(brain, "難しいです", mem=mem, coach=lambda t: study_plan.coached(plan, t),
                                      noted=note))
    asyncio.run(orchestrator.one_turn(brain, "難しいですね", mem=mem, coach=lambda t: study_plan.coached(plan, t),
                                      noted=note))
    assert brain.turns[0] == "難しいです"                              # turn 0: no note yet
    assert brain.turns[1].startswith("[coach:") and brain.turns[1].endswith("\n難しいですね")
    rows = [json.loads(l) for l in mem.log_path().read_text(encoding="utf-8").splitlines()]
    assert [r["student"]["text"] for r in rows] == ["難しいです", "難しいですね"] == noted
    assert not any("coach" in json.dumps(r, ensure_ascii=False) for r in rows)
    assert plan.vocab[0].item.text != "難しい" and plan.retired[0][0].text == "難しい"   # progressed


def test_the_note_goes_to_the_brain_and_never_into_the_transcript_in_the_voice_loop():
    class FakeStt:
        def listen(self, audio, quiet_rms=None):
            return Transcript(text="こんにちは。", accepted=True)

    class FakeVoice:
        def resume(self): ...
        async def say(self, chunk): ...
        async def drain(self): ...
        def cancel(self): ...

    class FakeVad:
        min_speech_ms = 300

        def enter(self, mode): ...

    brain, shown = FakeBrain("はい。"), []
    loop = VoiceLoop(turn_mode="vad", brain=brain, stt=FakeStt(), vad=FakeVad(), voice=FakeVoice(),
                     on_transcript=lambda t: shown.append(t.text), coach=lambda t: "[coach: elicit X next]\n" + t)
    asyncio.run(loop._turn(np.zeros(16000, dtype=np.float32)))
    assert brain.turns == ["[coach: elicit X next]\nこんにちは。"]
    assert loop.timings[0].transcript == "こんにちは。" and shown == ["こんにちは。"]


# ------------------------------------------------------------------- the summariser
def test_the_summariser_footer_and_the_topics_row_carry_what_progressed(tmp_path):
    mem = Memory(root=tmp_path / "memory", sessions=tmp_path / "sessions", session_id="past")
    mem.record_turn(student="", tutor_sentences=[{"text": "雨が降ったら行きます。", "grammar": [{"span": "降ったら", "point": "〜たら"}],
                                                  "target": "〜てみる"}])
    mem.record_turn(student="やってみます", tutor_sentences=[{"text": "はい。", "used": "〜てみる"}])
    mem.record_turn(student="食べてみます", tutor_sentences=[{"text": "いいですね。", "used": "〜てみる"}])
    footer = study_plan.summary_footer(mem.log_path(), 2)
    assert "TARGETS PRACTISED" in footer and "〜たら、〜てみる" in footer
    assert "TARGETS PROGRESSED" in footer and footer.rstrip().endswith("〜てみる")
    asked = []

    async def ask(text):
        asked.append(text)
        return json.dumps({"brief": "文法の練習。", "topics": ["雨"], "progressed": ["〜てみる"]}, ensure_ascii=False)

    now = Memory(root=tmp_path / "memory", sessions=tmp_path / "sessions", session_id="now")
    assert asyncio.run(now.summarise_pending(ask, "I", footer=lambda log: study_plan.summary_footer(log, 2))) == 1
    assert asked[0].rstrip().endswith("TARGETS PROGRESSED (produced correctly enough times to move on): 〜てみる")
    [row] = [json.loads(l) for l in now.topics_jsonl.read_text(encoding="utf-8").splitlines()]
    assert row["progressed"] == ["〜てみる"] and row["topics"] == ["雨"]
    assert now.sessions_summarised() == 1
    # an older summariser reply without the key still lands, with no key on the row
    assert now.apply_summary("old", "2026-08-01", {"brief": "x", "topics": ["y"]})
    assert "progressed" not in json.loads(now.topics_jsonl.read_text(encoding="utf-8").splitlines()[-1])


# ------------------------------------------------------------------------- config
def test_the_study_keys_exist_with_their_defaults_and_floors():
    by_key = {s.key: s for s in config.SCHEMA}
    for key, default, low in (("STUDY_TARGET_VOCAB", 8, 0), ("STUDY_TARGET_GRAMMAR", 4, 0),
                              ("STUDY_PROGRESS_AFTER", 2, 1), ("STUDY_NUDGE_EVERY", 3, 0),
                              ("STUDY_SPACING_BASE", 1, 1), ("STUDY_SPACING_MAX", 32, 1)):
        s = by_key[key]
        assert (s.default, s.type, s.low) == (default, int, low) and s.group and s.description
    cfg = config.load(env={})
    plan = study_plan.build(ITEMS, study_plan.fold([], ITEMS), cfg, session_index=0)
    assert len(plan.vocab) == 8 and len(plan.grammar) == 4 and plan.progress_after == 2
    assert plan.spacing_base == 1 and plan.spacing_max == 32 and plan.nudge_every == 3
