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


# ---------------------------------------------------------------- the grammar safety net
# 「と思います」 stayed black on the page although 「と思う」 was on the student's unsettled list
# (user, 2026-09-12): the tutor forgot her mark. The tokenizer finds the point by base forms.

POINTS = ["と思う", "ようだ", "あまり～ない", "〜てみる", "がする", "なら", "ば", "Verb[よう]", "〜ている"]


def found(text, points=POINTS, taken=()):
    a = annotate.Annotator(overrides={})
    return [(text[r["start"]:r["end"]], r["point"]) for r in a.find_points(text, points, taken)]


def test_an_inflected_point_is_found_by_its_base_form_through_the_auxiliary():
    assert found("明日は雨が降ると思います。") == [("と思います", "と思う")]
    assert found("食べてみようと思っています") == [("てみよう", "〜てみる"), ("と思っ", "と思う"), ("ています", "〜ている")]


def test_a_gap_in_the_points_name_may_hold_anything():
    assert found("あまり難しくないと思う") == [("あまり難しくない", "あまり～ない"), ("と思う", "と思う")]


def test_one_short_kana_token_is_never_guessed():
    assert found("雨なら行きません") == []               # なら, ば: her mark is the only way
    assert found("行けば分かります") == []


def test_her_own_mark_wins_where_they_overlap():
    assert found("雨が降ると思います", taken=[{"start": 4, "end": 7, "point": "と思う"}]) == []


def test_names_the_tokenizer_cannot_read_are_skipped_and_nothing_raises():
    assert found("はい、そうです。", ["Verb[よう]", "する (Have/Wear)", ""]) == []
    assert annotate.Annotator(overrides={}).find_points("", POINTS) == []


# --------------------------------------------------- furigana per kanji in a half-known compound
# 日本語 with 日 and 本 passed but 語 not: one reading over the run hid nothing. Split with
# WaniKani's readings for each kanji, the furigana is hidden over the two known ones alone (user,
# 2026-09-12: "hide the furigana for the kanji known in WaniKani").

WK = annotate.kana_table({"日": ["に", "にち", "ひ"], "本": ["ホン", "もと"], "語": ["ご"], "学": ["がく"],
                          "校": ["こう"], "花": ["か", "はな"], "火": ["か", "ひ"]})


def entries(a, text):
    return [(text[r["start"]:r["end"]], r["reading"], r["known"]) for r in a.readings(text)]


def test_a_half_known_run_is_split_per_kanji_with_wanikani_readings():
    a = annotate.Annotator(known_kanji={"日", "本"}, overrides={"日本語": "にほんご"}, kanji_readings=WK)
    assert entries(a, "日本語です。") == [("日", "に", True), ("本", "ほん", True), ("語", "ご", False)]


def test_rendaku_and_sokuon_are_allowed_between_kanji():
    assert annotate.split_reading("学校", "がっこう", WK) == ["がっ", "こう"]
    assert annotate.split_reading("花火", "はなび", WK) == ["はな", "び"]
    assert annotate.split_reading("日本", "にほん", WK) == ["に", "ほん"]
    assert annotate.split_reading("日本", "にっぽん", WK) == ["にっ", "ぽん"]
    assert annotate.split_reading("日々", "ひび", WK) == ["ひ", "び"]           # 々 repeats the kanji


def test_without_a_full_split_the_run_keeps_one_reading():
    table = {"日": ["にち"], "本": ["ほん"], "語": ["ご"]}                    # に is not a reading of 日
    assert annotate.split_reading("日本語", "にほんご", table) is None
    a = annotate.Annotator(known_kanji={"日", "本"}, overrides={"日本語": "にほんご"}, kanji_readings=table)
    assert entries(a, "日本語です。") == [("日本語", "にほんご", False)]


def test_a_run_wholly_known_or_wholly_unknown_is_not_split():
    both = annotate.Annotator(known_kanji={"日", "本", "語"}, overrides={"日本語": "にほんご"}, kanji_readings=WK)
    assert entries(both, "日本語") == [("日本語", "にほんご", True)]
    none = annotate.Annotator(overrides={"日本語": "にほんご"}, kanji_readings=WK)
    assert entries(none, "日本語") == [("日本語", "にほんご", False)]


def test_a_dotted_correction_splits_without_wanikani():
    """readings.txt may give a reading per kanji (に.ほん.ご) for the words WaniKani's readings
    cannot split — 日本 reads 日 as に, which is not one of its readings there."""
    a = annotate.Annotator(known_kanji={"日", "本"}, overrides={"日本語": "に.ほん.ご"})
    assert entries(a, "日本語") == [("日", "に", True), ("本", "ほん", True), ("語", "ご", False)]
    assert entries(annotate.Annotator(overrides={"日本語": "に.ほん.ご"}), "日本語") == [("日本語", "にほんご", False)]
    assert annotate.Annotator(known_kanji={"日"}, overrides={"日本語": "に.ほんご"}).readings("日本語")[0]["reading"] == "にほんご"


def test_the_shipped_corrections_split_japan_for_a_student_who_knows_its_kanji():
    a = annotate.Annotator(known_kanji={"日", "本"})                # the real backend/data/readings.txt
    assert entries(a, "日本語と日本人") == [("日", "に", True), ("本", "ほん", True), ("語", "ご", False),
                                      ("日", "に", True), ("本", "ほん", True), ("人", "じん", False)]


def test_wanikani_readings_are_normalised_to_hiragana_once():
    a = annotate.Annotator(kanji_readings={"本": ["ホン", "ほん", "", "もと"]})
    assert a.kanji_readings == {"本": ["ほん", "もと"]}
    a.kanji_readings = None
    assert a.kanji_readings == {}


# ---------------------------------------------------------------- tokens, for whole-word matching
def test_tokens_give_base_form_lemma_and_pos_over_code_points():
    """backend/study.py matches the student's words by token, not substring: 申す must never be
    the もう of もう一度, while たけ still reaches 竹 through the lemma (user, 2026-09-12)."""
    a = annotate.Annotator(overrides={})
    text = "𠮷野でもう一度食べます。"
    got = a.tokens(text)
    surfaces = [text[s:e] for s, e, *_ in got]
    assert "".join(surfaces) == text and "もう" in surfaces          # code points: 𠮷 shifts nothing
    assert "申す" not in [base for *_, base, _l, _p in got] and "申す" not in [l for *_, l, _p in got]
    by_surface = {text[s:e]: (base, lemma, pos) for s, e, base, lemma, pos in got}
    assert by_surface["食べ"] == ("食べる", "食べる", "動詞")
    assert by_surface["もう"][2] == "副詞" and by_surface["ます"][2] == "助動詞"
    assert a.tokens("") == []
    a._get = lambda: None
    assert a.tokens(text) == []
