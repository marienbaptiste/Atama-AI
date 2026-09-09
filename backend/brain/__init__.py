"""The brain interface (ADR-027).

Everything upstream of the chunker talks to a `Brain` and never imports a provider module.
`ClaudeCliBrain` is the only implementation (ADR-001 governs it); the interface exists so that
swapping in a local model or another vendor touches one directory instead of the pipeline.

Nothing in this module names Claude, MCP, or a CLI flag — that is the whole point. Three things
it deliberately does NOT assume, because they are where providers differ (ADR-027):

1. Conversation memory belongs to the provider. The caller sends the latest user turn, nothing
   more. A provider with server-side sessions resumes; one without keeps its own transcript.
2. Tool access is not MCP. Tools are plain Python functions over local state; how a provider
   exposes them (an MCP server, native function calling, or not at all) is its business.
3. Health is reported as `brain`, with the provider named in the detail.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class TextDelta:
    """A fragment of the assistant's spoken text, as it is generated."""

    text: str


@dataclass(frozen=True)
class Thinking:
    """The model reasoning before it speaks. Never spoken, never sent to TTS — but measured:
    time spent here is silence the student hears (spec §10)."""

    text: str


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolOutcome:
    name: str
    ok: bool
    summary: str = ""


@dataclass(frozen=True)
class RateLimited:
    """The provider is throttling. Not fatal: a fallback model may carry the conversation."""

    detail: str = ""
    active: bool = True


@dataclass(frozen=True)
class BrainError:
    message: str
    fatal: bool = False


@dataclass(frozen=True)
class TurnComplete:
    """End of one assistant turn, with the provider's own timing where it offers it."""

    text: str = ""
    ttft_ms: float | None = None
    duration_ms: float | None = None
    tool_ms: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)


BrainEvent = TextDelta | Thinking | ToolCall | ToolOutcome | RateLimited | BrainError | TurnComplete


@runtime_checkable
class Brain(Protocol):
    """One conversation with one model. Not thread-safe; one turn at a time."""

    #: Provider id for logs and the status chip, e.g. "claude-cli".
    name: str

    async def start(self) -> None:
        """Bring the brain up and make it ready to take a turn. Idempotent."""
        ...

    def turn(self, text: str) -> AsyncIterator[BrainEvent]:
        """Take one user turn; yield events until `TurnComplete` or a fatal `BrainError`.

        Named `turn`, not `send`: the Golden Rule gate (spec §0 rule 1) reads a bare `.send(` in
        any module that touches `backend.srs` as an HTTP write, and `turn` is the better word for
        one exchange anyway."""
        ...

    async def aclose(self) -> None:
        """Shut down and release resources. Safe to call twice."""
        ...


def create(cfg, registry=None, **kwargs) -> Brain:
    """Build the configured provider. The only place a provider module is imported."""
    provider = str(getattr(cfg, "BRAIN_PROVIDER", "claude-cli") or "claude-cli")
    if provider == "claude-cli":
        from backend.brain.claude_cli import ClaudeCliBrain

        return ClaudeCliBrain(cfg, registry=registry, **kwargs)
    raise ValueError(
        f"unknown BRAIN_PROVIDER {provider!r}. Implemented: 'claude-cli'. "
        "Adding a provider means implementing the Brain protocol in backend/brain/ (ADR-027)."
    )
