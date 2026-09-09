"""System prompt assembly: real files, section budgets, truncation (spec §6, ADR-012/026)."""
from __future__ import annotations

import pytest

from backend import prompt


def test_real_template_and_soul_assemble_within_budget():
    p = prompt.build("WaniKani level 4. Bunpro N4.")
    assert "{{soul}}" not in p.text and "{{student_profile}}" not in p.text
    assert "WaniKani level 4" in p.text
    assert p.tokens <= prompt.TOTAL_MAX_TOKENS
    assert p.truncated == []


def test_worst_case_sections_fit_the_total():
    """The real guard: a maxed soul AND a maxed profile must still fit.

    The budget test above uses a two-line profile, so it never noticed the static template
    growing. A student's profile grows as they study — the overflow would have arrived
    silently, months later, as a truncated profile (2026-09-09).
    """
    nl = chr(10)
    soul = nl.join(["あいうえおかきくけこ" * 5] * 40)
    profile = nl.join(["語彙: " + "漢字" * 40] * 40)
    p = prompt.build(profile, soul=soul)
    # truncation lands on a line boundary, so "at the cap" means within a line of it
    assert prompt.SOUL_MAX_TOKENS - 60 <= p.sections["soul"] <= prompt.SOUL_MAX_TOKENS
    assert prompt.PROFILE_MAX_TOKENS - 60 <= p.sections["student_profile"] <= prompt.PROFILE_MAX_TOKENS
    assert set(p.truncated) == {"soul", "student_profile"}
    assert p.tokens <= prompt.TOTAL_MAX_TOKENS, (
        f"template is {p.tokens - sum(p.sections.values())} tokens; trim it or raise the total")


def test_hard_output_rules_come_after_the_persona():
    """Position matters: the voice-pipeline rules must win over anything the soul file says."""
    text = prompt.build("profile").text
    assert text.index("WHO YOU ARE") < text.index("HARD OUTPUT RULES")


#: The shipped catalogue: persona -> the VOICEVOX style it was written for (ADR-030).
CATALOGUE = {"tanaka": 53, "hayashi": 67, "minami": 29, "mori": 14}


def test_each_persona_declares_the_voice_it_belongs_with(tmp_path):
    """Character and voice are one choice: a male persona in a female voice is jarring, and
    keeping them in two settings means they drift apart."""
    for name, style in CATALOGUE.items():
        assert prompt.declared_voice(name) == style, name
    assert prompt.declared_voice(tmp_path / "nothing.md") is None


def test_no_two_personas_share_a_voice():
    """Two tutors in the same voice are the same tutor wearing a different name."""
    assert len(set(CATALOGUE.values())) == len(CATALOGUE)


def test_the_personas_are_different_people():
    souls = {n: prompt.load_soul(name=n) for n in CATALOGUE}
    assert len(set(souls.values())) == len(souls)
    assert "たなか先生" in souls["tanaka"] and "はやし先生" in souls["hayashi"]
    assert "みなみ先生" in souls["minami"] and "ゆい" in souls["mori"]
    # Not one character re-skinned: different homes, different histories.
    assert "新潟" in souls["tanaka"] and "福岡" in souls["hayashi"] and "京都" in souls["minami"]


def test_the_conversation_partner_is_not_a_teacher():
    """`mori` earns her place by role, not by age — she is what you pick when being corrected
    every sentence is the problem. If she drifts into a 先生 she is redundant (ADR-030)."""
    mori = prompt.load_soul(name="mori")
    assert "「先生」ではありません" in mori
    assert "先生" not in mori.replace("「先生」ではありません", "")


def test_a_persona_name_resolves_into_prompts_and_a_path_is_taken_as_given(tmp_path):
    assert prompt.persona_path("minami") == prompt.PROMPTS_DIR / "minami.md"
    custom = tmp_path / "mine.md"
    assert prompt.persona_path(str(custom)) == custom


def test_an_unknown_persona_falls_back_to_a_neutral_tutor():
    assert prompt.load_soul(name="nobody-by-this-name") == prompt.NEUTRAL_SOUL


def test_soul_file_comments_are_not_sent_to_the_model(tmp_path):
    f = tmp_path / "soul.md"
    f.write_text("<!-- instructions for the human -->\n先生です。", encoding="utf-8")
    assert prompt.load_soul(f) == "先生です。"


def test_missing_soul_falls_back_to_a_neutral_tutor(tmp_path):
    assert prompt.load_soul(tmp_path / "absent.md") == prompt.NEUTRAL_SOUL


def test_the_voice_declaration_never_reaches_the_model():
    """`<!-- voice: 100 -->` is configuration, not character."""
    assert "voice:" not in prompt.load_soul(name="tanaka")
    assert "100" not in prompt.load_soul(name="tanaka")


def test_oversized_sections_are_truncated_at_a_line_boundary_and_reported():
    soul = "\n".join(["あいうえおかきくけこ" * 5] * 40)      # far over 400 tokens
    profile = "\n".join(["語彙: " + "漢字" * 40] * 40)        # far over 600 tokens
    p = prompt.build(profile, soul=soul)
    assert set(p.truncated) == {"soul", "student_profile"}
    assert p.sections["soul"] <= prompt.SOUL_MAX_TOKENS
    assert p.sections["student_profile"] <= prompt.PROFILE_MAX_TOKENS
    assert "\n".join(p.text.splitlines())  # still well-formed text


def test_empty_template_is_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="ADR-012"):
        prompt.build("profile", template="   ")


def test_placeholders_the_template_omits_are_simply_unused():
    p = prompt.build("PROFILE", soul="SOUL", template="only {{student_profile}} here")
    assert p.text == "only PROFILE here"
