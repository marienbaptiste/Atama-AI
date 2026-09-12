"""Furigana for the chat (spec §8b): readings per kanji run, corrections, and what the student knows.
Runs the real tokenizer (in-process, no network, no GPU)."""
from __future__ import annotations

from backend import annotate


def spans(text, readings):
    return [(text[r["start"]:r["end"]], r["reading"]) for r in readings]


def test_each_kanji_run_gets_its_own_reading_and_okurigana_stays_plain():
    a = annotate.Annotator(overrides={})
    text = "雨が降ったら、行きません。"
    assert spans(text, a.readings(text)) == [("雨", "あめ"), ("降", "ふ"), ("行", "い")]


def test_a_word_with_kana_between_its_kanji_is_split_per_kanji():
    assert annotate.align("取り消す", "とりけす") == [(0, 1, "と"), (2, 3, "け")]
    assert annotate.align("勉強", "べんきょう") == [(0, 2, "べんきょう")]


def test_the_corrections_file_wins_over_the_tokenizer():
    a = annotate.Annotator()                                   # the real backend/data/readings.txt
    text = "私は日本語と日本人が好きです。明日も。"
    got = dict(spans(text, a.readings(text)))
    assert got["私"] == "わたし" and got["日本語"] == "にほんご" and got["日本人"] == "にほんじん"
    assert got["明日"] == "あした" and got["好"] == "す"


def test_known_means_every_kanji_in_it_was_passed_on_wanikani():
    a = annotate.Annotator(known_kanji={"雨", "勉"}, overrides={})
    known = {r["reading"]: r["known"] for r in a.readings("雨の日に勉強する。")}
    assert known["あめ"] is True and known["べんきょう"] is False and known["ひ"] is False


def test_text_without_kanji_costs_nothing():
    assert annotate.Annotator(overrides={}).readings("はい、そうです。") == []


def test_without_a_tokenizer_there_is_less_furigana_never_an_error():
    a = annotate.Annotator(overrides={"日本": "にほん"})
    a._get = lambda: None                                      # fugashi missing
    text = "日本に行きます。"
    assert spans(text, a.readings(text)) == [("日本", "にほん")]


def test_katakana_becomes_hiragana():
    assert annotate.to_hiragana("ベンキョウー") == "べんきょうー"


# ------------------------------------------------------------- red is for grammar, not vocabulary
def mark(text, span, point=""):
    start = text.index(span)
    return {"start": start, "end": start + len(span), "point": point or span}


def test_a_plain_word_is_not_a_grammar_point():
    """The tutor marked a noun red (the user, 2026-09-12). Red is grammar, so the tokenizer has
    the last word: one noun, one name, one number, gone."""
    a = annotate.Annotator(overrides={})
    text = "竹の先生は東京で二時に話します。"
    for span in ("竹", "先生", "東京", "二時"):
        assert a.grammar_only(text, [mark(text, span)]) == [], span


def test_a_real_point_is_kept_however_short():
    a = annotate.Annotator(overrides={})
    for text, span in (("雨が降ったら行きません。", "降ったら"),
                       ("食べてみようと思います。", "食べてみよう"),
                       ("行くつもりです。", "つもり"),
                       ("読めますか。", "読めます"),
                       ("行かない。", "行かない")):
        assert a.grammar_only(text, [mark(text, span)]) == [mark(text, span)], span


def test_a_point_named_as_a_pattern_is_taken_at_its_word():
    """The tutor names points as Bunpro does. 〜中 over 勉強中 is every token a noun, and still
    grammar — the name says so, so the guard keeps it."""
    a = annotate.Annotator(overrides={})
    text = "勉強中です。"
    assert a.grammar_only(text, [mark(text, "勉強中", "〜中")]) == [mark(text, "勉強中", "〜中")]
    assert a.grammar_only(text, [mark(text, "勉強中", "study")]) == []


def test_without_a_tokenizer_every_mark_stands():
    a = annotate.Annotator(overrides={})
    a.error = "no dictionary"                     # _get() then returns None, as on a broken install
    text = "竹の先生。"
    assert a.grammar_only(text, [mark(text, "竹")]) == [mark(text, "竹")]
    assert a.grammar_only(text, []) == []
