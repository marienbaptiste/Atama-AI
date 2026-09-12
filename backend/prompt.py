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
#: 220 -> 400 on 2026-09-12: the two of them now remember each other — the student's name, where
#: they live, their cat, and everything the tutor has claimed about her own life (spec §6b). Ten
#: facts and six, one line each, is what "they have met before" costs.
MEMORY_MAX_TOKENS = 400
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
#: floated behind her (user). 2650 -> 2950 on 2026-09-12, three notes from the user in one
#: session: vocabulary was going red and conjugations were being missed (the mark rule now says
#: what grammar is, and lists the forms it forgot), the student was asked to produce grammar too
#: rarely (every second or third turn now), and her own new WaniKani words barely appeared (one or
#: two in every turn now). The template is ~1771 tokens of the total; the next section to grow
#: should pay for itself in the lesson, because this is latency (§10). 3250 -> 3400 on 2026-09-12
#: for "go through their grammar list, do not orbit the top of it" (user) — the profile now carries
#: every form still in their SRS, not two ghosts. The template is ~1985 tokens and the rules repeat
#: themselves in places: the next session's job is to rewrite them shorter (ROADMAP 22, item 6),
#: not to raise this again. 2950 -> 3150 the same day
#: for the memory section's own rise (see MEMORY_MAX_TOKENS): a tutor who forgets your name every
#: week is not a tutor you keep. 3150 -> 3250 the same evening, for the rule that stops her
#: INVENTING one — she greeted the student by a name nobody had told her (live, 2026-09-12).
#: The template is now ~1954 tokens and this has to stop: ROADMAP 22's next-session item 6 is to
#: rewrite these rules shorter, not to raise the ceiling again. It is latency (§10).
TOTAL_MAX_TOKENS = 3400
#: A ROTATED session's handoff (ADR-032): the lesson so far, from the turn log. Only rotated
#: sessions carry it, so it has its own budget on top of TOTAL_MAX_TOKENS rather than squeezing
#: the sections every session needs. Spent only when a long lesson has earned a fresh window.
HANDOFF_MAX_TOKENS = 600
#: The words over the handoff — what a rotated session is told about the lesson so far. A file,
#: like every other instruction the model reads (ADR-012).
HANDOFF_FILE = PROMPTS_DIR / "handoff.md"

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


def _fit(text: str, budget: int, label: str, truncated: list[str], keep: str = "head") -> str:
    """Truncate at a line boundary if over budget, and say so.

    `keep` says which end survives. The soul, the profile and the memory lead with what matters
    most, so their head is kept; a handoff is a transcript whose NEWEST lines are the ones the
    rotated session must carry on from, so its tail is kept — keeping the head handed her the
    start of a lesson she was supposed to be forty minutes into (2026-09-12).
    """
    if estimate_tokens(text) <= budget:
        return text
    lines = text.splitlines()
    if keep == "tail":
        lines = lines[::-1]
    kept: list[str] = []
    for line in lines:
        if estimate_tokens("\n".join([*kept, line])) > budget:
            break
        kept.append(line)
    if keep == "tail":
        kept.reverse()
    truncated.append(label)
    return "\n".join(kept).strip()


def handoff_heading() -> str:
    """What a rotated session is told over the lesson so far (prompts/handoff.md)."""
    text = _strip_comments(_read(HANDOFF_FILE)).strip()
    if not text:
        raise RuntimeError(f"missing or empty handoff heading at {HANDOFF_FILE} (ADR-012: it lives in a file)")
    return text


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
    handoff_text = (_fit(handoff.strip(), HANDOFF_MAX_TOKENS, "handoff", truncated, keep="tail")
                    if handoff.strip() else "")
    memory_slot = "\n\n".join(s for s in (memory_text, f"{handoff_heading()}\n{handoff_text}" if handoff_text else "") if s)

    text = (tpl.replace("{{soul}}", soul_text)
               .replace("{{student_profile}}", profile_text)
               .replace("{{memory}}", memory_slot))
    sections = {"soul": estimate_tokens(soul_text), "student_profile": estimate_tokens(profile_text),
                "memory": estimate_tokens(memory_text), "handoff": estimate_tokens(handoff_text)}
    total = estimate_tokens(text)
    return RenderedPrompt(text=text, tokens=total, sections=sections, truncated=truncated)
