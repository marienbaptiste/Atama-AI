"""Their own words in a sentence (spec §8b, user 2026-09-12): blue is what they are still learning.
Hermetic — the snapshot is already in memory, and nothing here fetches (ADR-024)."""
from __future__ import annotations

from backend import study
from backend.srs import bunpro as bp
from backend.srs import profile as profile_api
from backend.srs import wanikani as wk


def vocab(characters, stage, reading="", meaning=""):
    return wk.Vocab(characters=characters, reading=reading, meaning=meaning, level=10,
                    srs_stage=stage, incorrect=0)


def point(title):
    return bp.GrammarPoint(title=title, meaning="", jlpt="N4", srs="ghost")


def a_profile(**over):
    w = wk.WaniKaniProfile(level=12)
    w.recent_unlocks = over.get("unlocks", [vocab("勉強", 2, "べんきょう", "study"),
                                            vocab("公園", 1, "こうえん", "park"),
                                            vocab("先生", 7, "せんせい", "teacher")])
    w.leeches = over.get("leeches", [vocab("難しい", 3, "むずかしい", "difficult")])
    b = bp.BunproProfile()
    b.ghosts = over.get("ghosts", [point("〜たら")])
    b.weak_grammar = over.get("weak", [point("〜てみる")])
    return profile_api.StudentProfile(wanikani=w, bunpro=b)


def test_only_what_they_have_not_gurued_counts():
    s = study.Study.from_profile(a_profile())
    words = {i.text for i in s.items if i.kind == "vocab"}
    assert words == {"勉強", "公園", "難しい"}         # 先生 is stage 7: learned, so not marked
    assert {i.text for i in s.items if i.kind == "grammar"} == {"〜たら", "〜てみる"}


def test_their_words_are_found_where_they_appear():
    s = study.Study.from_profile(a_profile())
    text = "公園で勉強するのは難しいですか。"
    assert [(text[x["start"]:x["end"]]) for x in s.spans(text)] == ["公園", "勉強", "難しい"]
    assert s.spans("今日はいい天気ですね。") == []


def test_the_longest_word_wins_and_spans_never_overlap():
    s = study.Study(items=[study.Item("勉強", "vocab"), study.Item("勉強する", "vocab")])
    text = "毎日勉強するのが好きです。"
    spans = s.spans(text)
    assert [text[x["start"]:x["end"]] for x in spans] == ["勉強する"]


def test_a_one_character_word_is_not_marked():
    """Marking every 人 and 日 would paint the whole conversation blue."""
    s = study.Study.from_profile(a_profile(unlocks=[vocab("人", 1), vocab("日本語", 2)], leeches=[]))
    assert {i.text for i in s.items if i.kind == "vocab"} == {"日本語"}


def test_what_the_tutor_credits_is_checked_against_their_lists():
    """The float behind her is for something they are still learning, so `[used:…]` is looked up."""
    s = study.Study.from_profile(a_profile())
    assert s.kind_of("勉強") == "vocab"
    assert s.kind_of("〜たら") == "grammar"
    assert s.kind_of("たら") == "grammar"              # she writes it both ways
    assert s.kind_of("「〜てみる」") == "grammar"
    assert s.kind_of("先生") == ""                     # learned months ago: not a win
    assert s.kind_of("") == ""


def test_no_srs_data_is_no_colour_and_no_crash():
    s = study.Study.from_profile(profile_api.StudentProfile())
    assert s.items == [] and s.spans("勉強します。") == [] and s.kind_of("勉強") == ""
    assert study.Study.from_profile(None).items == []


# --------------------------------------------------- she writes them as she likes (user feedback)
def test_a_word_counts_however_she_writes_it():
    """13 % of her sentences carried one of their words when only the exact spelling matched
    (measured from the turn logs, 2026-09-12): kana and inflections were being missed."""
    s = study.Study([study.Item("気に入る", "vocab", "きにいる"),
                     study.Item("勉強する", "vocab", "べんきょうする"),
                     study.Item("難しい", "vocab", "むずかしい"),
                     study.Item("竹の子", "vocab", "たけのこ")])
    for text, word in (("この本が気に入りました。", "気に入る"),
                       ("毎日勉強しています。", "勉強する"),
                       ("漢字は難しかったですね。", "難しい"),
                       ("たけのこが好きです。", "竹の子")):
        spans = s.spans(text)
        assert [x["word"] for x in spans] == [word], (text, spans)


def test_a_stem_too_short_to_be_a_word_is_not_matched():
    """Dropping the ending must not leave a single character that appears everywhere."""
    s = study.Study([study.Item("見る", "vocab", "みる")])
    assert study.written_forms(study.Item("見る", "vocab", "みる")) == ["見る", "みる"]
    assert s.spans("見せてください。") == []


def test_everything_below_guru_is_in_play_not_only_the_newest():
    w = wk.WaniKaniProfile(level=12)
    w.in_progress = [vocab("公園", 1), vocab("勉強", 2), vocab("先生", 6)]
    w.recent_unlocks = [vocab("公園", 1)]
    s = study.Study.from_profile(profile_api.StudentProfile(wanikani=w))
    assert {i.text for i in s.items if i.kind == "vocab"} == {"公園", "勉強"}
