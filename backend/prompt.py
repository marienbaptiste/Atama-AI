"""System prompt assembly (spec §6, ADR-012/026).

Three versioned files under `prompts/` plus one runtime value, never hardcoded in Python:

    prompts/tutor.md   the template, with {{soul}} and {{student_profile}} placeholders
    prompts/soul.md    who Sensei is (optional)
    <runtime>          the Student Profile rendered from WaniKani + Bunpro

Each section has a token budget, because a prompt that grows silently is latency that grows
silently (§10, ADR-011). Over-budget sections are truncated at a line boundary and reported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from backend import config
from backend.srs.profile import estimate_tokens

PROMPTS_DIR = config.REPO_ROOT / "prompts"
TEMPLATE_FILE = PROMPTS_DIR / "tutor.md"
SOUL_FILE = PROMPTS_DIR / "soul.md"

SOUL_MAX_TOKENS = 400
PROFILE_MAX_TOKENS = 600
#: Whole assembled prompt. Template is ~700 tokens, so this leaves room for both sections.
TOTAL_MAX_TOKENS = 2000

NEUTRAL_SOUL = "あなたは経験のある、あたたかい日本語の先生です。"


@dataclass
class RenderedPrompt:
    text: str
    tokens: int
    sections: dict[str, int] = field(default_factory=dict)
    truncated: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.text


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _strip_comments(text: str) -> str:
    """Drop HTML comments — the soul file's own instructions are for the user, not the model."""
    out, depth, i = [], 0, 0
    while i < len(text):
        if text.startswith("<!--", i):
            depth += 1
            i += 4
        elif text.startswith("-->", i):
            depth = max(0, depth - 1)
            i += 3
        else:
            if depth == 0:
                out.append(text[i])
            i += 1
    return "".join(out)


def _fit(text: str, budget: int, label: str, truncated: list[str]) -> str:
    """Truncate at a line boundary if over budget, and say so."""
    if estimate_tokens(text) <= budget:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    for line in lines:
        if estimate_tokens("\n".join([*kept, line])) > budget:
            break
        kept.append(line)
    truncated.append(label)
    return "\n".join(kept).rstrip()


def load_soul(path: Path | None = None) -> str:
    """Sensei's persona, or a neutral default when the file is absent (ADR-026)."""
    text = _strip_comments(_read(path or SOUL_FILE)).strip()
    return text or NEUTRAL_SOUL


def build(student_profile: str, *, soul: str | None = None, template: str | None = None) -> RenderedPrompt:
    """Assemble the system prompt. Placeholders that the template omits are simply not used."""
    tpl = template if template is not None else _read(TEMPLATE_FILE)
    if not tpl.strip():
        raise RuntimeError(f"missing or empty prompt template at {TEMPLATE_FILE} (ADR-012: it lives in a file)")

    truncated: list[str] = []
    soul_text = _fit((soul if soul is not None else load_soul()).strip(), SOUL_MAX_TOKENS, "soul", truncated)
    profile_text = _fit(student_profile.strip(), PROFILE_MAX_TOKENS, "student_profile", truncated)

    text = tpl.replace("{{soul}}", soul_text).replace("{{student_profile}}", profile_text)
    sections = {"soul": estimate_tokens(soul_text), "student_profile": estimate_tokens(profile_text)}
    total = estimate_tokens(text)
    return RenderedPrompt(text=text, tokens=total, sections=sections, truncated=truncated)
