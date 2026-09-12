"""The WebSocket protocol, defined once (spec §8).

This module is the single source of truth for what may travel between the orchestrator and the
browser. `frontend/src/protocol.gen.ts` is GENERATED from it (`python -m backend.tools.gen_protocol`)
— every type, field and literal — and `test_models.py` fails while the committed copy is stale, so
a rename that breaks the browser fails CI here rather than appearing as a silent no-op in the UI
three days later. On the page, `ws.ts` types its dispatcher so `tsc` fails while any server type
has no handler (gate M3a).

Frozen at M2 on purpose (ROADMAP subsystem 7): M3 is then pure frontend against a fixed contract.
One addition at M3 (2026-09-11): `state.turn`, the barge-in epoch (see `Speak.turn`).
Pruned on 2026-09-12: types that nothing sent or nothing handled (`audio_chunk` — the orchestrator
captures the microphone itself, spec §9; `settings_test`; `assistant_text`; `emotion`;
`srs_profile`; `control: bargein_ack`) are gone rather than kept as a contract nobody honours.

Two conventions worth knowing before adding a message:

* `type` is a `Literal`, and the unions below are discriminated on it. Adding a model without
  adding it to its union means it parses as "unknown" — the union is what makes it real.
* Nothing here reaches for a secret. `settings` echoes are built by the settings layer, which
  replaces every secret with `{set, hint}` before it gets this far (ADR-022, §11). Models cannot
  enforce that on their own, so the test suite checks the echo path instead.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Msg(BaseModel):
    """Reject unknown keys: a typo in a field name should fail loudly, like a typo in a type."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------- client -> server
class Control(_Msg):
    """Turn-taking and session control.

    `start`/`stop` are the push-to-talk edges (spec §9): under `TURN_MODE=ptt` the key press IS
    the turn boundary, so these map to `VoiceLoop.ptt_begin()` / `ptt_end()`. Under `vad` they
    are advisory and the silence window still decides.
    """

    type: Literal["control"] = "control"
    #: `quit` shuts everything down cleanly — the claude subprocess, the speech queue, this
    #: socket, and then the containers, because the page's stop button means "I am done for today"
    #: (user, 2026-09-12). Ctrl+C in the terminal leaves the containers up on purpose. It is NOT `stop`, which is already the push-to-talk release edge; overloading
    #: it would make every released key a request to exit.
    #: `new_topic` is the page's "New topic" button: she drops the current subject and finds a
    #: fresh one, exactly as if the student had said 「話題を変えて」.
    #: `cancel`: ALT GR during a recording — what was captured is dropped and the talk key can
    #: be released without sending anything (spec §8). It is not `stop`, which sends the turn.
    #: The server sends the same `cancel` on the student's behalf when the page holding the key
    #: drops off the socket (backend/app.py `Hub.leave`), so a lost tab never wedges the mic.
    #: `ready`: the page has been touched, so the browser will let it play sound (autoplay
    #: policy). The server holds her voice until a page says so.
    action: Literal["start", "stop", "cancel", "resync", "quit", "new_topic", "ready"]


class SettingsUpdate(_Msg):
    """Partial update of any key in the `config.py` schema, secrets included (§11).

    The server validates, persists to `settings.json`, applies live where it can, and replies
    with a `Settings` echo in which secrets appear only as `{set, hint}`.
    """

    type: Literal["settings"] = "settings"
    values: dict[str, Any]


class Explain(_Msg):
    """The student clicked something and wants it explained (spec §8b, ADR-036): a red grammar
    point, or a sentence's translate icon. Nothing is generated until this arrives."""

    type: Literal["explain"] = "explain"
    kind: Literal["grammar", "sentence"]
    #: The grammar point, or the sentence to translate.
    text: str
    #: For a grammar point: the sentence it was used in, so the answer is about this use.
    context: str = ""
    #: Explanation language (EXPLAIN_LANGUAGE); a translation is always English.
    lang: Literal["en", "ja"] = "en"



# --------------------------------------------------------------- server -> client
class State(_Msg):
    type: Literal["state"] = "state"
    state: Literal["listening", "thinking", "speaking"]
    #: The turn epoch this state belongs to (see `Speak.turn`): a page that interrupts her while
    #: she is still thinking knows which sentences, not yet arrived, to drop.
    turn: int = 0


class SttPartial(_Msg):
    """RESERVED and never emitted in M2-M5.

    STT runs on complete utterances, so there are no partials to send. The type exists so that a
    streaming-STT experiment later is not a protocol change (spec §8). A test asserts the server
    never emits it, which is the only way a reserved type stays reserved.
    """

    type: Literal["stt_partial"] = "stt_partial"
    text: str


class Reading(_Msg):
    """Furigana for one run of kanji (spec §8b): characters [start, end) of the text, in code points,
    and its reading in hiragana. `known`: the student has passed every kanji in it on WaniKani — the
    page hides those by default (FURIGANA=unknown)."""

    start: int
    end: int
    reading: str
    known: bool = False


class VocabSpan(_Msg):
    """Where a word the student is still learning appears in a sentence (spec §8b, user
    2026-09-12): characters [start, end) in code points, blue on the page. From the WaniKani
    snapshot already on disk (backend/study.py) — no model call, no fetch.

    The card rides along: `reading`, `meaning` and `stage` are what WaniKani already knows about
    this item, so clicking the word costs nothing and answers instantly (user, 2026-09-12). A few
    dozen bytes per span; the alternative is a round trip per click for data we already hold."""

    start: int
    end: int
    word: str
    #: Kana reading, English meaning, and the WaniKani SRS stage name ("Apprentice 2"). Empty when
    #: the word came from somewhere without them.
    reading: str = ""
    meaning: str = ""
    stage: str = ""


class SttFinal(_Msg):
    type: Literal["stt_final"] = "stt_final"
    text: str
    #: False when the hallucination filter rejected it; `reason` says which rule fired (§9).
    accepted: bool = True
    reason: str = ""
    #: Furigana for the chat (backend/annotate.py), computed on the orchestrator, no model call.
    readings: list[Reading] = []
    #: Their own words, in what they just said — the same blue as in hers.
    vocab: list[VocabSpan] = []


class GrammarSpan(_Msg):
    """Where she used a grammar point in a `speak` sentence (ADR-036): characters [start, end) of
    `text`, counted in Unicode code points — the page counts the same way (Array.from)."""

    start: int
    end: int
    point: str


class Speak(_Msg):
    """One sentence of synthesised audio with its lip-sync timeline.

    This is the message the whole pipeline exists to produce. `vtimes`/`vdurations` are in
    **milliseconds** — verified against TalkingHead's `speakAudio` (V0.4, 2026-09-10); seconds
    would be a silent 1000x drift. `visemes` are bare Oculus ids (`aa`, `PP`), not the
    `viseme_`-prefixed morph target names.

    `emotion` is applied when this sentence's audio STARTS, not when the message arrives (§8).
    """

    type: Literal["speak"] = "speak"
    audio_b64: str
    visemes: list[str]
    vtimes: list[float]
    vdurations: list[float]
    text: str
    emotion: str = ""
    #: The turn epoch (backend/app.py `Hub.epoch`): it goes up when a turn starts and when she is
    #: interrupted, so the page drops audio of a turn it has already barged in on however late
    #: that audio arrives, and never mistakes the next turn's for it.
    turn: int = 0
    #: Study marks (ADR-036, spec §8b), never spoken: her grammar uses in this sentence, and what
    #: she wants the student to use next when this sentence asks for it (the page's hint).
    grammar: list[GrammarSpan] = []
    target: str = ""
    #: A word or grammar point the student has just used correctly (user, 2026-09-12): the page
    #: floats it up behind her, the way her mood faces drift.
    used: str = ""
    #: "vocab" | "grammar" | "": which list `used` came from, checked against what the student has
    #: NOT yet Guru'd (backend/study.py). It colours the float, and an empty string means the tutor
    #: named something that is not in their lists.
    used_kind: str = ""
    #: Furigana for the chat (backend/annotate.py), computed on the orchestrator, no model call.
    readings: list[Reading] = []
    #: Their own words in this sentence, blue on the page (backend/study.py).
    vocab: list[VocabSpan] = []


class BargeIn(_Msg):
    """The server has accepted an interruption: stop playback and drop queued audio of `turn` and
    every epoch before it."""

    type: Literal["bargein"] = "bargein"
    turn: int = 0


class ServiceStatus(_Msg):
    """One chip in the status bar (§5b). `last_error` is already sanitised by the registry —
    never a token, never a URL containing one."""

    type: Literal["service_status"] = "service_status"
    service: str
    state: str
    detail: str = ""
    last_error: str = ""


class Settings(_Msg):
    """Echo of the applied settings. Secrets appear ONLY as `{set: true, hint: "…abcd"}`; the
    stored value never returns to the browser (ADR-022)."""

    type: Literal["settings"] = "settings"
    values: dict[str, Any]
    #: The config schema the page is generated from (backend/settings_view.py). Not `schema`:
    #: that name shadows a pydantic BaseModel method.
    fields: list[dict[str, Any]] = []
    #: Keys set in the environment (`ATAMA_*`), which win over anything saved from the page.
    pinned: dict[str, str] = {}
    #: In a reply to an update: which keys were written, and why the others were refused.
    saved: list[str] = []
    errors: dict[str, str] = {}


class MicLevel(_Msg):
    """Microphone level for the settings panel's meter — at most ~10 a second (spec §9).
    `level` is RMS (peak-held between sends); `speech` is the VAD's probability."""

    type: Literal["mic_level"] = "mic_level"
    level: float
    speech: float = 0.0


class Meters(_Msg):
    """The status bar's gauges (user request, 2026-09-10). Every field is optional: each arrives
    when its source reports — context and use after a turn, GPU memory on the heartbeat — and the
    server merges them, so a page always receives the latest of each."""

    type: Literal["meters"] = "meters"
    #: How full her context is: tokens the last request read, of the model's window.
    context_tokens: int | None = None
    context_window: int | None = None
    #: Turns with her this calendar month. A subscription has no bill, and the CLI reports no
    #: monthly figure — turns are the honest count (backend/usage.py).
    month_turns: int | None = None
    #: The CLI's rate-limit window: status ("allowed", ...), kind ("five_hour"), reset (epoch s).
    limit_status: str | None = None
    limit_type: str | None = None
    limit_resets_at: float | None = None
    vram_used_mib: int | None = None
    vram_total_mib: int | None = None


class Timing(_Msg):
    """Per-turn stage breakdown (§10), so p50/p90 are measurable rather than anecdotal."""

    type: Literal["timing"] = "timing"
    stt_ms: float = 0.0
    first_chunk_ms: float = 0.0
    first_audio_ms: float = 0.0
    #: Her first sentence STARTS PLAYING (first_audio_ms is synthesis done): the honest
    #: voice-to-voice number, 0.0 when nothing played (2026-09-12).
    first_play_ms: float = 0.0
    total_ms: float = 0.0
    barged_in: bool = False
    #: The Claude stage taken apart: time to first token and thinking before speaking.
    ttft_ms: float | None = None
    thinking_chars: int = 0
    #: Rolling over this session, so the page can show where the p90 stands against the gate.
    p50_ms: float | None = None
    p90_ms: float | None = None
    turns: int = 0


class Explanation(_Msg):
    """The answer to one `explain`, or why there is none. Cached on the orchestrator, so asking
    the same thing twice costs nothing."""

    type: Literal["explanation"] = "explanation"
    kind: Literal["grammar", "sentence"]
    #: Echo of what was asked, so the page can match the answer to what the student clicked.
    text: str
    answer: str = ""
    error: str = ""


class Error(_Msg):
    """A problem the user should see. An unknown client message produces one of these rather
    than an exception — a malformed frame must not take the socket down."""

    type: Literal["error"] = "error"
    message: str
    fatal: bool = False


# ------------------------------------------------------------------------ unions
ClientMessage = Annotated[
    Union[Control, SettingsUpdate, Explain],
    Field(discriminator="type"),
]

ServerMessage = Annotated[
    Union[State, SttPartial, SttFinal, Speak, BargeIn, ServiceStatus, Settings, MicLevel, Meters,
          Timing, Explanation, Error],
    Field(discriminator="type"),
]


#: Parse an incoming frame into the right model, or raise. The discriminator does the work, so an
#: unknown `type` fails here rather than three layers down as a missing attribute.
ClientMessageAdapter = TypeAdapter(ClientMessage)
ServerMessageAdapter = TypeAdapter(ServerMessage)


def _types(union) -> frozenset[str]:
    """The `type` literals a union accepts — derived, never hand-listed, so it cannot drift."""
    args = union.__origin__.__args__ if hasattr(union, "__origin__") else union.__args__
    return frozenset(m.model_fields["type"].default for m in args)


CLIENT_TYPES = _types(ClientMessage)
SERVER_TYPES = _types(ServerMessage)
#: Sent by neither side in M2-M5; see SttPartial.
RESERVED_TYPES = frozenset({"stt_partial"})
