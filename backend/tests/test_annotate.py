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
