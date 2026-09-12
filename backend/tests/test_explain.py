"""Explanations and translations on click (spec §8b, ADR-036 point 2): asked once, then cached,
and never fatal. The worker itself is injected, so these run with no model and no network."""
from __future__ import annotations

import asyncio

from backend import config, explain


def cfg(tmp_path):
    return config.load(tmp_path / "settings.json", env={"CACHE_DIR": str(tmp_path)})


def explainer(tmp_path, ask):
    return explain.Explainer(cfg(tmp_path), ask=ask)


def test_an_answer_is_asked_once_and_served_from_the_cache_after(tmp_path):
    asked = []

    async def ask(prompt):
        asked.append(prompt)
        return "  It marks a condition: when A happens, B.  "

    e = explainer(tmp_path, ask)
    first = asyncio.run(e.explain("grammar", "〜たら", "雨が降ったら、行きません。", "en"))
    second = asyncio.run(e.explain("grammar", "〜たら", "雨が降ったら、行きません。", "en"))
    assert first == ("It marks a condition: when A happens, B.", "")
    assert second == first
    assert len(asked) == 1 and e.asked == 1 and e.served == 1      # the second click is free
    assert "〜たら" in asked[0] and "English" in asked[0] and "雨が降ったら" in asked[0]


def test_the_language_is_part_of_the_question_and_of_the_cache(tmp_path):
    asked = []

    async def ask(prompt):
        asked.append(prompt)
        return "説明。"

    e = explainer(tmp_path, ask)
    asyncio.run(e.explain("grammar", "〜たら", "", "en"))
    asyncio.run(e.explain("grammar", "〜たら", "", "ja"))
    assert len(asked) == 2 and "English" in asked[0] and "Japanese" in asked[1]


def test_a_translation_is_the_sentence_whatever_it_was_clicked_from(tmp_path):
    assert (explain.cache_key("sentence", "雨。", "one", "en")
            == explain.cache_key("sentence", "雨。", "another", "en"))
    assert (explain.cache_key("grammar", "〜たら", "one", "en")
            != explain.cache_key("grammar", "〜たら", "another", "en"))
    assert "Translate" in explain.prompt_for("sentence", "雨が降ったら。", "", "ja")


def test_a_failure_is_a_message_the_page_can_show(tmp_path):
    async def ask(_prompt):
        raise RuntimeError("model unavailable")

    answer, error = asyncio.run(explainer(tmp_path, ask).explain("grammar", "〜たら"))
    assert answer == "" and "RuntimeError" in error and "model unavailable" in error


def test_an_empty_answer_is_not_cached_as_one(tmp_path):
    replies = ["", "  ", "Here it is."]

    async def ask(_prompt):
        return replies.pop(0)

    e = explainer(tmp_path, ask)
    assert asyncio.run(e.explain("sentence", "雨。"))[1] == "no answer came back"
    assert asyncio.run(e.explain("sentence", "雨。"))[1] == "no answer came back"
    assert asyncio.run(e.explain("sentence", "雨。")) == ("Here it is.", "")   # it retried each time


def test_there_is_nothing_to_explain_about_nothing(tmp_path):
    async def ask(_prompt):
        raise AssertionError("must not be asked")

    assert asyncio.run(explainer(tmp_path, ask).explain("grammar", "   ")) == ("", "nothing to explain")


def test_the_cache_survives_a_restart(tmp_path):
    async def ask(_prompt):
        return "cached answer"

    asyncio.run(explainer(tmp_path, ask).explain("grammar", "〜たら"))

    async def never(_prompt):
        raise AssertionError("must not be asked again")

    fresh = explainer(tmp_path, never)                   # a new session, the same cache directory
    assert asyncio.run(fresh.explain("grammar", "〜たら")) == ("cached answer", "")
