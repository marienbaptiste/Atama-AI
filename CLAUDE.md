# Working on atama-AI

> **GOLDEN RULE (spec §0): NEVER set or write anything through the WaniKani or Bunpro API
> keys.** Read-only, always, everywhere — orchestrator, fetchers, MCP server, tests, scripts.
> Do not write a function in `backend/srs/` whose name starts with `set_`, `write_`, `update_`,
> `create_`, `delete_`, `submit_`, `start_`, `post_`, `put_`, `patch_`, `mark_`, `reset_` or
> `assign_`, whatever it does. Do not import an HTTP library anywhere in `srs/` except
> `srs/http.py`. Do not add a fourth MCP tool. Do not add any URL literal to `srs/` other than
> the two pinned origins, and never make a base URL configurable. Do not touch
> `backend/tools/readonly_gate.py`.
> The gate runs on every `make test` / `make run` / `make doctor` / commit and has no bypass —
> if it fails, fix the code, never the gate. If a task seems to require a write, stop and tell
> the user; the answer is no (ADR-021).

Instructions for Claude Code sessions in this repo. Read this first; it tells you which of the
other documents to open, when, and what each one is authoritative for.

**Project in one line:** a local-first, real-time voice Japanese tutor with a 3D avatar, whose
brain is the `claude` CLI running headless as a persistent subprocess.

**Current state: M0 done, M1 done.** The read-only gate, config/settings store, status registry,
GET-only SRS client, WaniKani + Bunpro fetchers, profile renderer, Bunpro MCP server, sentence
chunker, prompt assembly, the `Brain` interface with its Claude CLI provider, and the text REPL
all exist with tests (`make test`). Verified live: Sensei answers in character, uses the MCP
tools, and weaves in the student's ghost reviews.

Next: **M2** (mic → VAD → Whisper → VOICEVOX → playback). Do not start it until M1's gate is
signed off in `ROADMAP.md`.

Run the tutor: `.venv/Scripts/python -m backend.repl` (`--refresh` to re-sync SRS, `--no-srs`
offline). V0 spikes still open: V0.3 VOICEVOX, V0.4 TalkingHead, V0.6 VRAM, V0.11 SearxNG.

---

## Which file to read, and for what

| File | Authoritative for | Open it when |
|------|-------------------|--------------|
| **[ATAMA-AI_SPEC.md](ATAMA-AI_SPEC.md)** | **Everything.** Architecture, data flow, pinned stack, subprocess rules (§4) and login flow (§4b), SRS read-only rule (§5) and status indicators (§5b), prompt template, emotion→voice (§7) and emotion→rig (§8), self-barge-in, latency and VRAM budgets, config/settings/hygiene (§11), milestones, repo layout, global don'ts, platform topology (§15). | Before writing any code at all, and again at the start of every milestone. This is the contract. |
| **[ROADMAP.md](ROADMAP.md)** | *How* to build and prove each subsystem: verification spikes (V0, with the findings log), per-subsystem tests, validation steps, integration order, gates, standing regression suite. | Whenever you are about to start, test, or claim completion of a subsystem or milestone. |
| **[ADR.md](ADR.md)** | *Why* each pinned choice was made, its cost, and what would reverse it. ADR-016 (subprocess isolation), ADR-021 (read-only SRS) and ADR-022 (settings interface) are user directives. | Before proposing any change to the stack or architecture, and whenever a decision looks arbitrary or wrong. |
| **[README.md](README.md)** | The user-facing description: setup, login, config, troubleshooting. | When you change setup, config surface, ports, commands, protocol, or milestone status — then update it. |
| **[.env.example](.env.example)** | The complete inventory of configuration keys with defaults. The settings page and `config.py` must list the same keys. | When adding or renaming any config key. |
| **`prompts/tutor.md`** | Sensei's personality, teaching behaviour, emotion-tag usage and voice-output rules. | When tutor behaviour is the topic. Never move this content into Python (ADR-012). |
| **`backend/constants.py`** | Verified external-interface findings, dated. | Before calling any external interface. If it is not pinned there, verify it first (ADR-015). |

**Precedence when documents disagree:** `ATAMA-AI_SPEC.md` > `ADR.md` > `ROADMAP.md` >
`README.md`. A lower-precedence file that contradicts a higher one is a bug in that file — fix it.

---

## Before you start any task

1. **Read the spec section that covers the task.** The spec is dense and section-numbered; find
   the section rather than working from memory. Subprocess → §4/§4b. SRS + status → §5/§5b.
   TTS/visemes/emotion voice → §7. Frontend/protocol/emotion rig/barge-in → §8. STT/VAD → §9.
   Latency → §10. VRAM → §10b. Settings/hygiene → §11. Platform → §15. Don'ts → §14.
2. **Check `ROADMAP.md` for that subsystem's entry** — it tells you what tests exist, what has to
   be validated live, and what gate you are working toward.
3. **Check `ADR.md`** if the task touches a stack choice. The answer to "why not just use X" is
   almost certainly already written down.

## Before you finish any task

1. **Run the subsystem's tests** from the roadmap entry. Golden and unit tests are hermetic — no
   network, no GPU, no external process.
2. **Check the gate.** A milestone is not done because the code runs; it is done when its gate in
   `ROADMAP.md` is met. Report gate status honestly, including failures and skipped steps.
3. **Update the docs you invalidated.** Changed a port, a command, a config key, or the WS
   protocol → `README.md` and `.env.example`. Changed a decision → a new ADR entry plus a
   `Superseded by` mark on the old one. Changed how something is built or proven → `ROADMAP.md`.

---

## Rules that apply to every session

These come from spec §4, §5, §11 and §14 and are not negotiable without asking the user.

**Never set or write through the WaniKani or Bunpro API keys** (Golden Rule, spec §0).
Read-only, enforced at token, client and tool level (ADR-021), and verified at every
compilation by `backend/tools/readonly_gate.py`: first target of `make test`, prerequisite of
`make run` and `make doctor`, import-time self-check, runtime `ReadOnlyTransport`, frontend
`prebuild` grep, pre-commit hook. The SRS HTTP client has only `get()`. The Bunpro MCP server
exposes exactly three read tools. No setter-shaped name in `srs/`. No write tool behind any
flag, ever. Never edit the gate. **Never call the SRS APIs outside app launch and manual
Refresh** (ADR-024): no timers, no per-turn fetches; the MCP server reads the snapshot and
holds no token.

**Never fabricate an external interface.** Not CLI flags, not endpoint schemas, not library
signatures. Verify against `claude --help`, VOICEVOX's live `/docs` OpenAPI, the TalkingHead
README, or the WaniKani docs — then pin the finding as a dated comment plus a constant
(ADR-015, ROADMAP V0). If you cannot verify it, stop and say so.

**Never substitute the Anthropic API or SDK for the `claude` CLI subprocess.** Subscription auth
is a hard requirement (ADR-001). The subprocess is spawned with `--tools ""`,
`--strict-mcp-config`, an orchestrator-assigned `--session-id`, from an empty cwd, with an
**allowlisted** environment; `init.apiKeySource` must be `"none"` (ADR-016). Never `--bare`,
never `--dangerously-skip-permissions`, never `--no-session-persistence`. The cwd is
`config.claude_cwd()` — **outside the repo**, never `.cache/` inside it. **Wait for the MCP
ready marker before the first turn; never `sleep` for it** (ROADMAP findings, 2026-09-09).

**Never bind to `0.0.0.0`.** Loopback only, all three services (ADR-017).

**Never require a `.env` and never send a stored secret back to the browser.** The settings
page is the configuration interface; env vars are an optional override; secrets echo as
`{set, hint}` (ADR-022).

**Never swap a pinned stack piece without asking** — no React, no cloud TTS, no cloud STT, no GPU
VOICEVOX. Each is an ADR with reasoning; if you think one is wrong, propose superseding it.

**Never gold-plate.** No auth system, no multi-user, no database. Files and one user (ADR-013).

**Never commit** secrets, `.env`, `settings.json`, `mcp.json`, the avatar GLB, model weights, or
fixtures holding personal SRS data beyond the sanitised golden files. `make check-secrets` is
part of `make test`.

**Build milestones strictly in order.** M0 → M1 → M2 → M3 → M4 → M5. Do not skip; do not start the
next one while the previous gate is unmet.

**The two hard numbers are gates, not aspirations.** Voice→voice p90 ≤ 3.0 s, VRAM ≤ 10 GB. They
are measured from M2 onward. Fillers mask latency, they do not meet the budget — log true
first-content latency separately (ADR-008).

**Emotion goes end to end.** A tag in Claude's text must reach both the voice (VOICEVOX style
and params) and the face (TalkingHead mood / blendshapes), applied when that sentence's audio
starts — and must never reach the TTS text (ADR-020).

**When reality contradicts the spec, ask the user.** Then update the spec, then the ADR if a
decision changed, then the code. In that order. Do not silently diverge (spec §14).

---

## Repo conventions

- Layout is fixed by spec §13. Put files where it says. Generated and personal files go under
  `.cache/` or `logs/`, never next to source.
- All tunables resolve through `backend/config.py` (defaults → `settings.json` → env). No magic
  numbers scattered through the code.
- Data that changes without a release — the Whisper hallucination blocklist, the filler pool —
  lives in `backend/data/`, not in Python.
- WS message types are defined once and mirrored in pydantic models; a contract test asserts both
  sides agree.
- The mora→viseme mapper and the emotion→voice table are pure functions with golden/table tests.
  Keep them pure.
- Every subsystem reports to the status registry (`backend/status.py`); error strings are
  sanitised there, once, not at each call site.
- Session logs are the user's future Anki mine: one clean JSONL record per turn, stable schema.
