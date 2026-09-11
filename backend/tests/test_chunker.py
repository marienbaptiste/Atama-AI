"""Sentence chunker: split points, delta boundaries, emotion tags (ROADMAP subsystem 2)."""
from __future__ import annotations

import pytest

from backend.chunker import NEUTRAL, Chunk, SentenceChunker, strip_tag


def run(deltas: list[str]) -> tuple[list[Chunk], SentenceChunker]:
    c = SentenceChunker()
    out: list[Chunk] = []
    for d in deltas:
        out += c.push(d)
    out += c.close()
    return out, c


def texts(chunks: list[Chunk]) -> list[str]:
    return [c.text for c in chunks]


# ------------------------------------------------------------------ splitting
def test_splits_on_japanese_terminators():
    out, _ = run(["こんにちは。元気ですか？はい！"])
    assert texts(out) == ["こんにちは。", "元気ですか？", "はい！"]


def test_splits_on_newline_and_ascii_marks():
    out, _ = run(["一行目\n二行目です。Really?"])
    assert texts(out) == ["一行目", "二行目です。", "Really?"]


def test_does_not_split_mid_sentence_across_delta_boundaries():
    """The terminator arrives in its own delta — the sentence must still come out whole, once."""
    out, _ = run(["よく", "でき", "まし", "た", "。", "でも", "ここ", "は", "違います", "。"])
    assert texts(out) == ["よくできました。", "でもここは違います。"]


# ------------------------------------------------------------------ study marks (ADR-036)
def test_a_grammar_mark_is_never_spoken_and_lands_on_its_words():
    out, c = run(["[happy]雨が{{降ったら|〜たら}}行きません。"])
    [chunk] = out
    assert chunk.text == "雨が降ったら行きません。" and chunk.emotion == "happy"
    [g] = chunk.grammar
    assert chunk.text[g.start:g.end] == "降ったら" and g.point == "〜たら"
    assert c.stray_marks == []


def test_marks_split_across_deltas_come_out_whole():
    out, _ = run(["雨が{{降っ", "たら|〜た", "ら}}、{{行かない|〜ない}}", "です。"])
    [chunk] = out
    assert chunk.text == "雨が降ったら、行かないです。"
    assert [(chunk.text[g.start:g.end], g.point) for g in chunk.grammar] == [("降ったら", "〜たら"), ("行かない", "〜ない")]


def test_a_target_at_the_head_of_a_question_rides_on_that_sentence():
    out, _ = run(["はい。[encouraging][target:〜たら]『たら』を使って答えてみてください。"])
    assert texts(out) == ["はい。", "『たら』を使って答えてみてください。"]
    assert out[0].target == "" and out[1].target == "〜たら" and out[1].emotion == "encouraging"


def test_a_target_before_the_emotion_tag_works_too():
    out, c = run(["[target:〜てもいい][encouraging]聞いてみてください。"])
    assert out[0].text == "聞いてみてください。" and out[0].target == "〜てもいい"
    assert out[0].emotion == "encouraging" and c.stray_tags == []


def test_a_target_split_across_deltas_is_never_spoken():
    out, _ = run(["[tar", "get:〜た", "ら]使ってみて。"])
    assert texts(out) == ["使ってみて。"] and out[0].target == "〜たら"


def test_a_target_anywhere_else_is_still_removed():
    out, _ = run(["使ってみてください[target:〜たら]。"])
    assert texts(out) == ["使ってみてください。"] and out[0].target == "〜たら"


def test_a_broken_mark_never_reaches_the_voice():
    for broken in ("雨が{{降ったら行きません。", "雨が降ったら|〜たら}}行きません。", "雨が降ったら}}行きません。"):
        out, c = run([broken])
        assert texts(out) == ["雨が降ったら行きません。"], broken
        assert out[0].grammar == () and c.stray_marks, broken


def test_a_mark_in_the_turn_log_names_its_words():
    out, _ = run(["[encouraging][target:〜たら]雨が{{降ったら|〜たら}}？"])
    assert out[0].as_log() == {"text": "雨が降ったら？", "emotion": "encouraging", "synth_ms": None,
                               "grammar": [{"span": "降ったら", "point": "〜たら"}], "target": "〜たら"}


def test_a_plain_sentence_logs_as_before():
    out, _ = run(["はい。"])
    assert out[0].as_log() == {"text": "はい。", "emotion": None, "synth_ms": None}


def test_period_is_not_a_split_point():
    """`.` is ambiguous (decimals, abbreviations) and is not in the spec's split set."""
    out, _ = run(["It costs 3.50 dollars today"])
    assert texts(out) == ["It costs 3.50 dollars today"]


def test_ellipsis_terminates_without_leaving_an_empty_chunk():
    out, _ = run(["ええと…そうですね。"])
    assert texts(out) == ["ええと…", "そうですね。"]


@pytest.mark.parametrize("tail", ["。\n", "…。", "……", "。。", "\n\n", "！？"])
def test_runs_of_terminators_never_produce_a_punctuation_only_chunk(tail):
    out, _ = run([f"はい{tail}つぎ。"])
    assert texts(out) == [f"はい{tail}".strip(), "つぎ。"]


def test_close_flushes_a_turn_that_never_terminated():
    out, _ = run(["終わらない文"])
    assert texts(out) == ["終わらない文"]


def test_close_emits_nothing_when_there_is_nothing_speakable():
    out, _ = run(["。", "\n", "  "])
    assert out == []


# -------------------------------------------------------------------- emotion
def test_leading_tag_is_stripped_and_attached():
    out, _ = run(["[happy]よくできました。"])
    assert out == [Chunk("よくできました。", "happy")]


def test_tag_split_across_deltas_is_still_recognised():
    out, _ = run(["[hap", "py]", "よくできました。"])
    assert out == [Chunk("よくできました。", "happy")]


def test_tag_at_each_sentence_start_switches_emotion():
    out, _ = run(["[happy]いいですね。[serious]でも違います。もう一度。"])
    assert out == [
        Chunk("いいですね。", "happy"),
        Chunk("でも違います。", "serious"),
        Chunk("もう一度。", "serious"),  # inherits until the next tag
    ]


def test_untagged_turn_is_neutral():
    out, c = run(["そうですね。"])
    assert out == [Chunk("そうですね。", NEUTRAL)] and c.emotion == NEUTRAL


def test_mid_sentence_tag_never_reaches_tts_and_applies_to_the_next_sentence():
    out, c = run(["いいですね[serious]。ここは違います。"])
    assert texts(out) == ["いいですね。", "ここは違います。"]
    assert [x.emotion for x in out] == [NEUTRAL, "serious"]
    assert c.stray_tags == ["serious"]  # visible as a prompt bug, not silently swallowed


def test_unknown_bracket_text_is_left_alone():
    """Only the four known tags are special; anything else is the model's words."""
    out, c = run(["[こんにちは]と言います。"])
    assert texts(out) == ["[こんにちは]と言います。"] and c.stray_tags == []


def test_repeated_leading_tags_keep_the_last():
    out, _ = run(["[happy][serious]どうぞ。"])
    assert out == [Chunk("どうぞ。", "serious")]


def test_unterminated_partial_tag_at_end_of_turn_is_not_spoken():
    out, _ = run(["はい。", "[hap"])
    assert texts(out) == ["はい。"]


# ------------------------------------------------------------------- property
@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 11])
def test_concatenation_reproduces_input_minus_tags_at_any_delta_size(chunk_size):
    src = "[happy]よくできました。[thinking]でも、ここは少し違います。もう一度言ってみてください。"
    deltas = [src[i : i + chunk_size] for i in range(0, len(src), chunk_size)]
    out, _ = run(deltas)
    expected = src.replace("[happy]", "").replace("[thinking]", "")
    assert "".join(texts(out)) == expected
    assert [c.emotion for c in out] == ["happy", "thinking", "thinking"]


def test_no_chunk_ever_contains_a_bracket_tag():
    out, _ = run(["[happy]あ。い[serious]。[thinking]う。"])
    assert all("[" not in c.text for c in out)


# ---------------------------------------------------------------- convenience
def test_strip_tag_helper():
    assert strip_tag("[surprised]すごい") == ("すごい", "surprised")
    assert strip_tag("すごい") == ("すごい", NEUTRAL)
