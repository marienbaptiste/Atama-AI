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
def fake_tokens(text):
    """A hand-tokenised sentence: (start, end, base, lemma, pos) — what Annotator.tokens gives."""
    table = {
        "この本が気に入りました。": [("この", "この", "此の", "連体詞"), ("本", "本", "本", "名詞"), ("が", "が", "が", "助詞"),
                                   ("気に入り", "気に入る", "気に入る", "動詞"), ("まし", "ます", "ます", "助動詞"),
                                   ("た", "た", "た", "助動詞"), ("。", "。", "。", "補助記号")],
        "毎日勉強しています。": [("毎日", "毎日", "毎日", "名詞"), ("勉強", "勉強", "勉強", "名詞"), ("し", "する", "為る", "動詞"),
                               ("て", "て", "て", "助詞"), ("い", "いる", "居る", "動詞"), ("ます", "ます", "ます", "助動詞"),
                               ("。", "。", "。", "補助記号")],
        "漢字は難しかったですね。": [("漢字", "漢字", "漢字", "名詞"), ("は", "は", "は", "助詞"), ("難しかっ", "難しい", "難しい", "形容詞"),
                                 ("た", "た", "た", "助動詞"), ("です", "です", "です", "助動詞"), ("ね", "ね", "ね", "助詞"),
                                 ("。", "。", "。", "補助記号")],
        "たけが好きです。": [("たけ", "たけ", "竹", "名詞"), ("が", "が", "が", "助詞"), ("好き", "好き", "好き", "形状詞"),
                           ("です", "です", "です", "助動詞"), ("。", "。", "。", "補助記号")],
        "もう一度言ってもらえますか。": [("もう", "もう", "もう", "副詞"), ("一度", "一度", "一度", "名詞"), ("言っ", "言う", "言う", "動詞"),
                                     ("て", "て", "て", "助詞"), ("もらえ", "もらえる", "貰える", "動詞"), ("ます", "ます", "ます", "助動詞"),
                                     ("か", "か", "か", "助詞"), ("。", "。", "。", "補助記号")],
        "見せてください。": [("見せ", "見せる", "見せる", "動詞"), ("て", "て", "て", "助詞"), ("ください", "くださる", "下さる", "動詞"),
                           ("。", "。", "。", "補助記号")],
    }
    out, pos = [], 0
    for surface, base, lemma, p in table[text]:
        start = text.index(surface, pos)
        pos = start + len(surface)
        out.append((start, pos, base, lemma, p))
    return out


def test_a_word_counts_however_she_writes_it():
    """13 % of her sentences carried one of their words when only the exact spelling matched
    (measured from the turn logs, 2026-09-12): inflections and kana were being missed. The
    tokenizer's base form and lemma catch both, by whole tokens."""
    s = study.Study([study.Item("気に入る", "vocab", "きにいる"),
                     study.Item("勉強する", "vocab", "べんきょうする"),
                     study.Item("難しい", "vocab", "むずかしい"),
                     study.Item("竹", "vocab", "たけ")], tokens=fake_tokens)
    for text, word, seen in (("この本が気に入りました。", "気に入る", "気に入り"),
                             ("毎日勉強しています。", "勉強する", "勉強し"),
                             ("漢字は難しかったですね。", "難しい", "難しかっ"),
                             ("たけが好きです。", "竹", "たけ")):
        spans = s.spans(text)
        assert [(x["word"], text[x["start"]:x["end"]]) for x in spans] == [(word, seen)], (text, spans)


def test_a_piece_of_a_token_is_never_a_word():
    """申す reads もうす; the もう of もう一度 is not it (user, 2026-09-12, screenshot). No reading,
    no stem: a word is a whole token or nothing."""
    s = study.Study([study.Item("申す", "vocab", "もうす"), study.Item("見る", "vocab", "みる")], tokens=fake_tokens)
    assert s.spans("もう一度言ってもらえますか。") == []
    assert s.spans("見せてください。") == []
    assert not hasattr(study, "written_forms")


def test_without_a_tokenizer_only_the_written_form_counts():
    s = study.Study([study.Item("申す", "vocab", "もうす"), study.Item("勉強する", "vocab", "べんきょうする")])
    assert s.spans("もう一度、申すそうです。") == [{"start": 5, "end": 7, "word": "申す", "reading": "もうす",
                                            "meaning": "", "stage": ""}]
    assert s.spans("毎日勉強しています。") == []           # an inflection needs the tokenizer


def test_everything_below_guru_is_in_play_not_only_the_newest():
    w = wk.WaniKaniProfile(level=12)
    w.in_progress = [vocab("公園", 1), vocab("勉強", 2), vocab("先生", 6)]
    w.recent_unlocks = [vocab("公園", 1)]
    s = study.Study.from_profile(profile_api.StudentProfile(wanikani=w))
    assert {i.text for i in s.items if i.kind == "vocab"} == {"公園", "勉強"}
