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


def test_hard_output_rules_come_after_the_persona():
    """Position matters: the voice-pipeline rules must win over anything the soul file says."""
    text = prompt.build("profile").text
    assert text.index("WHO YOU ARE") < text.index("HARD OUTPUT RULES")


def test_soul_file_comments_are_not_sent_to_the_model(tmp_path):
    f = tmp_path / "soul.md"
    f.write_text("<!-- instructions for the human -->\n先生です。", encoding="utf-8")
    assert prompt.load_soul(f) == "先生です。"


def test_missing_soul_falls_back_to_a_neutral_tutor(tmp_path):
    assert prompt.load_soul(tmp_path / "absent.md") == prompt.NEUTRAL_SOUL


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
