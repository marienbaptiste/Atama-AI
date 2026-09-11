"""System prompt assembly (spec §6, ADR-012/026).

Three versioned files under `prompts/` plus one runtime value, never hardcoded in Python:

    prompts/tutor.md   the template, with {{soul}} and {{student_profile}} placeholders
    prompts/soul.md    who Sensei is (optional)
    <runtime>          the Student Profile rendered from WaniKani + Bunpro

Each section has a token budget, because a prompt that grows silently is latency that grows
silently (§10, ADR-011). Over-budget sections are truncated at a line boundary and reported.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from backend import config
from backend.srs.profile import estimate_tokens

PROMPTS_DIR = config.REPO_ROOT / "prompts"
TEMPLATE_FILE = PROMPTS_DIR / "tutor.md"
#: minami, not tanaka: she is the persona with a usable avatar, and a default that cannot
#: render a face is not a default. Changing this changes the voice too, by design (ADR-030).
DEFAULT_PERSONA = "minami"
#: A persona may declare the voice it belongs with: `<!-- voice: 53 -->` on its own line.
#: Character and voice are one choice — switching tutor should not mean remembering to change a
#: speaker id as well, and a mismatch (a male persona in a female voice) is jarring.
_VOICE_DECLARATION = re.compile(r"<!--\s*voice:\s*(\d+)\s*-->")
_AVATAR_DECLARATION = re.compile(r"<!--\s*avatar:\s*([A-Za-z0-9_.-]+\.glb)\s*-->")

SOUL_MAX_TOKENS = 400
PROFILE_MAX_TOKENS = 600
#: Cross-session memory (spec §6b, ADR-031): last-session brief, recent topics, student notes.
#: Deliberately small — it is read on every turn as cached prompt, so its cost is tokens, not
#: latency, and the point is a tutor who remembers the gist, not one who replays the transcript.
MEMORY_MAX_TOKENS = 220
#: Whole assembled prompt. Template is ~700 tokens, so this leaves room for both sections.
#: Ceiling for template + both sections. Raised 2000 -> 2100 on 2026-09-09: the explicit
#: correction policy and the elicitation rule grew the static template to ~1050 tokens, and at
#: 2000 a fully-grown student profile would have overflowed. Prompt size is a §10 latency lever,
#: so this is a deliberate spend, not headroom to fill — test_worst_case_sections_fit_the_total
#: keeps it honest.
#: Raised again 2100 -> 2350 on 2026-09-10 for the memory section. The same trade as before:
#: a deliberate spend, enforced by test_worst_case_sections_fit_the_total, not headroom to fill.
#: Raised 2350 -> 2550 on 2026-09-11 for the study-mark rules (ADR-036): the tutor tags her grammar
#: and what she wants the student to use, so the page can show them with no extra model call. Same
#: trade, same test. 2550 -> 2650 on 2026-09-12 for the `[used:]` mark — what the student got right,
#: floated behind her (user). The template is ~1465 tokens of the total.
TOTAL_MAX_TOKENS = 2650
#: A ROTATED session's handoff (ADR-032): the lesson so far, from the turn log. Only rotated
#: sessions carry it, so it has its own budget on top of TOTAL_MAX_TOKENS rather than squeezing
#: the sections every session needs. Spent only when a long lesson has earned a fresh window.
HANDOFF_MAX_TOKENS = 600
HANDOFF_HEADING = ("EARLIER IN THIS LESSON — you and the student were already talking; this is the "
                   "latest of it. Carry on naturally from here: do not greet again or restart.")

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


def persona_path(name: str | None = None) -> Path:
    """`"minami"` -> prompts/minami.md. An absolute or relative path is taken as given."""
    name = str(name or DEFAULT_PERSONA).strip() or DEFAULT_PERSONA
    candidate = Path(name)
    if candidate.suffix or candidate.is_absolute() or len(candidate.parts) > 1:
        return candidate if candidate.is_absolute() else (config.REPO_ROOT / candidate)
    return PROMPTS_DIR / f"{name}.md"


def declared_voice(name_or_path: str | Path | None = None) -> int | None:
    """The VOICEVOX style id this persona is written for, if it names one."""
    path = name_or_path if isinstance(name_or_path, Path) else persona_path(name_or_path)
    match = _VOICE_DECLARATION.search(_read(path))
    return int(match.group(1)) if match else None


def declared_avatar(name_or_path: str | Path | None = None) -> str | None:
    """The GLB this persona is written for, if it names one.

    The face belongs to the same choice as the voice (ADR-026): たなか先生 is a fifty-year-old man
    and putting him behind a teenager's face is exactly as jarring as putting him in her voice.
    So the persona declares both, and switching `TUTOR_PERSONA` moves the whole character.
    """
    path = name_or_path if isinstance(name_or_path, Path) else persona_path(name_or_path)
    match = _AVATAR_DECLARATION.search(_read(path))
    return match.group(1) if match else None


def avatar_dir() -> Path:
    return config.REPO_ROOT / "frontend" / "public"


def resolved_avatar(name_or_path: str | Path | None = None) -> tuple[str | None, bool]:
    """(GLB actually usable for this persona, whether it is the one they declared).

    A persona declares the face it is written for, but only one avatar is commissioned so far.
    Rendering nothing because たなか has no model of his own is the wrong failure: the lesson is
    the point, and the wrong face is better than no face. So an absent declaration falls back to
    whichever avatar does exist, and the flag says it is a stand-in — callers can say so rather
    than pretending.

    Drop the real `tanaka.glb` in later and this starts returning it, with no code change.
    """
    declared = declared_avatar(name_or_path)
    directory = avatar_dir()
    if declared and (directory / declared).is_file():
        return declared, True
    # Prefer the DEFAULT persona's face as the stand-in, not whatever sorts first: the default is
    # the one guaranteed to be present (`make avatar` fetches it), so the fallback is predictable
    # rather than alphabetical.
    fallback = declared_avatar(DEFAULT_PERSONA)
    if fallback and (directory / fallback).is_file():
        return fallback, False
    for candidate in sorted(directory.glob("*.glb")):
        return candidate.name, False
    return None, False


def load_soul(path: Path | None = None, name: str | None = None) -> str:
    """Sensei's persona, or a neutral default when the file is absent (ADR-026)."""
    text = _strip_comments(_read(path or persona_path(name))).strip()
    return text or NEUTRAL_SOUL


def build(student_profile: str, *, soul: str | None = None, template: str | None = None,
          persona: str | None = None, memory: str = "", handoff: str = "") -> RenderedPrompt:
    """Assemble the system prompt. Placeholders that the template omits are simply not used.

    `handoff` is for a rotated session only (ADR-032): it follows the memory section under its own
    heading and budget, so the template needs no placeholder for it."""
    tpl = template if template is not None else _read(TEMPLATE_FILE)
    if not tpl.strip():
        raise RuntimeError(f"missing or empty prompt template at {TEMPLATE_FILE} (ADR-012: it lives in a file)")

    truncated: list[str] = []
    soul_text = _fit((soul if soul is not None else load_soul(name=persona)).strip(), SOUL_MAX_TOKENS, "soul", truncated)
    profile_text = _fit(student_profile.strip(), PROFILE_MAX_TOKENS, "student_profile", truncated)
    memory_text = _fit(memory.strip(), MEMORY_MAX_TOKENS, "memory", truncated) if memory.strip() else ""
    handoff_text = _fit(handoff.strip(), HANDOFF_MAX_TOKENS, "handoff", truncated) if handoff.strip() else ""
    memory_slot = "\n\n".join(s for s in (memory_text, f"{HANDOFF_HEADING}\n{handoff_text}" if handoff_text else "") if s)

    text = (tpl.replace("{{soul}}", soul_text)
               .replace("{{student_profile}}", profile_text)
               .replace("{{memory}}", memory_slot))
    sections = {"soul": estimate_tokens(soul_text), "student_profile": estimate_tokens(profile_text),
                "memory": estimate_tokens(memory_text), "handoff": estimate_tokens(handoff_text)}
    total = estimate_tokens(text)
    return RenderedPrompt(text=text, tokens=total, sections=sections, truncated=truncated)
