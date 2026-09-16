# Contributing to atama-AI

This is a local-first, real-time voice Japanese tutor: one user, no accounts, no database, files
on disk. Its brain is the `claude` CLI running headless as a persistent subprocess.

Two things make this repo different from most, and both are deliberate:

1. **The spec is the contract.** [ATAMA-AI_SPEC.md](ATAMA-AI_SPEC.md) is section-numbered and
   dense. Code that contradicts it is a bug in the code, not a new convention.
2. **The read-only rule is enforced by a gate, not by trust.** It runs on every test, every
   launch and every commit, and there is no way to switch it off.

---

## Read before you change anything

| File | What it decides | Open it when |
|------|-----------------|--------------|
| [ATAMA-AI_SPEC.md](ATAMA-AI_SPEC.md) | Architecture, data flow, the pinned stack, budgets, milestones, repo layout | Before writing code, and at the start of every milestone |
| [ADR.md](ADR.md) | Why each choice was made, what it cost, what would reverse it | Before proposing a change to the stack or the architecture |
| [ROADMAP.md](ROADMAP.md) | How each subsystem is built and proven: tests, validation steps, gates, the live checks still pending | Before you start, test, or claim a subsystem is done |
| [README.md](README.md) | The user-facing description: setup, configuration, troubleshooting | When you change setup, config, ports, commands or the protocol |
| [CLAUDE.md](CLAUDE.md) | The same rules, written for Claude Code sessions in this repo | If you work with an agent here |

When the documents disagree, the order above is the precedence:
`ATAMA-AI_SPEC.md` > `ADR.md` > `ROADMAP.md` > `README.md`. The lower file is the one to fix.

For any change, the useful sequence is: find the spec section that covers it, read that
subsystem's entry in the roadmap, then check whether an ADR already answers "why not just do X".
Usually one does.

---

## The Golden Rule

**The app reads from WaniKani and Bunpro. It never writes to them.** Not from the orchestrator,
not from a fetcher, not from Claude, not from a test, not from a script, not temporarily.

In practice, inside `backend/srs/`:

- The HTTP client exposes `get()` and nothing else.
- No function or attribute may be named `set_*`, `write_*`, `update_*`, `create_*`, `delete_*`,
  `submit_*`, `start_*`, `post_*`, `put_*`, `patch_*`, `mark_*`, `reset_*` or `assign_*`,
  whatever it actually does.
- Only `srs/http.py` may import an HTTP library.
- The two service origins are constants. A configurable base URL is exactly how a token ends up
  at the wrong host, so there is no setting for it.
- No SRS tool is exposed to the model at all (ADR-039). WaniKani and Bunpro reach the tutor
  through the Student Profile in her prompt.
- The APIs are called only at launch and on the user's Refresh (ADR-024). No timers, no per-turn
  fetches.

`backend/tools/readonly_gate.py` checks this statically on every `make test`, `make run`,
`make doctor` and `git commit`, and `frontend/scripts/srs-grep.mjs` does the browser half on every
`npm run build` and `npm test`. **Never edit the gate to make a change pass.** If a task seems to
need a write, stop and ask; the answer is no.

---

## Setup

Prerequisites: Python 3.11+, Node 20+, Docker with the engine running, an NVIDIA GPU with CUDA,
and the `claude` CLI logged in with a subscription (never an API key). On Linux you also need
`libportaudio2`. See [README.md](README.md#prerequisites) for the full list and the supported
platforms.

```bash
python -m venv .venv          # then activate it
pip install -e ".[dev]"
make hooks                    # installs the pre-commit gate; do this before your first commit
make avatar                   # fetches the default avatar (not committed, see LICENSE section 1)
make doctor                   # one PASS/WARN/FAIL line per dependency, with what to do
```

On Windows there is no `make`. Use `.\run` and `.\stop` for the two common ones, and call the
rest directly, for example `.venv\Scripts\python -m backend.tools.doctor`.

---

## The commands

| Command | What it does |
|---------|--------------|
| `make test` | The gate, the secret scan, then pytest. This is the one to run before a commit |
| `make check-readonly` | The Golden Rule gate alone |
| `make check-secrets` | Refuses a leftover `.env` and scans for committed secrets |
| `make doctor` | Checks the machine: CLI, auth, ports, containers, tokens, GPU, avatar |
| `make run` / `.\run` | Containers, the tutor and the page, in one command |
| `make stop` / `.\stop` | Takes the containers down (the app leaves them up on Ctrl+C) |
| `make avatar` | Fetches the default GLB |
| `make capture-bunpro` | Re-captures live Bunpro responses into `.cache/` for fixture pinning |

The page lives in `frontend/` (Vite and vanilla TypeScript, no framework, ADR-009):

```bash
cd frontend
npm ci                  # once
npm run dev             # hot reload on 127.0.0.1:5173, proxied to a running tutor
npm test                # Vitest, headless
npm run build           # type-check, then build into frontend/dist
```

**The WebSocket protocol is generated, not mirrored.** It is defined once in
`backend/models.py`. After changing a message there, run:

```bash
.venv/Scripts/python -m backend.tools.gen_protocol
```

`test_models.py` fails while the committed `frontend/src/protocol.gen.ts` is stale, and `tsc`
fails while any server message has no handler on the page.

---

## Tests

- **Everything in `backend/tests/` and `frontend/src/*.test.ts` is hermetic:** no network, no
  GPU, no external process, no real `claude`. The fake CLI is `backend/tests/fake_claude.py`, and
  recorded responses live in `backend/tests/fixtures/`.
- **Name a test after the behaviour it protects,** as a sentence. Existing names read like
  `test_a_timed_out_turn_does_not_leak_its_late_result_into_the_next`. A reviewer should be able
  to tell what broke from the name alone.
- **Prove a new test earns its place.** Put the bug back, watch the test fail, then restore the
  fix. A test that passes against the broken code protects nothing.
- **Pure things stay pure.** The mora-to-viseme mapper and the emotion-to-voice table are pure
  functions with golden and table tests. Keep them that way.
- There is no CI. The pre-commit hook runs the gate and the secret scan; running `make test` and
  the frontend tests before you push is on you.

### Live checks are not tests

A milestone is done when its gate in [ROADMAP.md](ROADMAP.md) is met, not when the code runs.
Several things can only be proved on real hardware, in a real lesson: latency percentiles, VRAM,
barge-in, the microphone, the avatar. Those are listed under **Live checks pending** in the
roadmap. If you build something whose proof is live, add it to that list rather than describing
it as working, and report gate status honestly, failures and skipped steps included.

---

## Conventions

- **Layout is fixed by spec §13.** Put files where it says. Generated and personal files go under
  `.cache/` or `logs/`, never beside source.
- **Every tunable resolves through `backend/config.py`** (defaults, then `settings.json`, then
  `ATAMA_*` environment variables). Its schema is the whole key inventory, and the settings page
  is generated from it. No magic numbers scattered through the code, and no `.env`: the app does
  not read one.
- **Words the model reads live in `prompts/`,** never in Python (ADR-012). That includes the
  tutor's rules, each persona, the summariser's instructions and the study plan's wording.
- **Data that changes without a release lives in `backend/data/`:** the hallucination blocklist,
  the model tiers, the news feeds, the scenarios.
- **Every subsystem reports to the status registry** (`backend/status.py`), and error strings are
  sanitised there once, not at each call site. A token must never reach a chip, a log or the page.
- **Comments record the why, with a date,** especially when the code looks odd. Most of the odd
  lines here are a measurement or a live failure someone already paid for.
- **Never fabricate an external interface.** Not a CLI flag, not an endpoint shape, not a library
  signature. Verify it against `claude --help`, VOICEVOX's live OpenAPI, the TalkingHead README or
  the WaniKani docs, then pin the finding as a dated constant in `backend/constants.py`
  (ADR-015). If you cannot verify it, say so instead of guessing.
- **Bind to loopback only** (ADR-017). The one exception is the phone page (`backend/remote.py`,
  ADR-041), which binds one address of the machine, HTTPS only, off by default.
- **Do not gold-plate.** No auth system, no multi-user, no database (ADR-013).
- **Do not swap a pinned stack piece without asking:** no React, no cloud TTS or STT, no GPU
  VOICEVOX, and never the Anthropic API in place of the `claude` CLI subprocess (ADR-001).

---

## Changing a decision

The stack choices are recorded with their reasoning, so disagreeing with one is fine, but it is a
document change before it is a code change. In this order:

1. **Spec first.** Update the section that describes the behaviour.
2. **Then the ADR.** Add a new entry with the context, the decision, the cost and what would
   reverse it, and mark the old entry `Superseded by ADR-0NN` rather than rewriting it. History
   stays readable.
3. **Then the code**, then the roadmap entry that says how it is proven, then the README if you
   changed a command, a port, a config key or the protocol.

When reality contradicts the spec, ask rather than diverging quietly.

---

## Commits

Commit messages here are prose, not a tag format. The subject says what changed, in a sentence
that means something on its own:

```
Her memory of a lesson is written as it ends, in seconds, and the stop button really stops
The Bunpro MCP server is retired: both SRS sources reach her through the profile only (ADR-039)
```

The body explains **why**, and what it cost to learn: the failure that prompted it, the
measurement that settled it, what was verified and what was not. Reference the ADR or spec
section when one is involved. If a change came from using the app, say what happened.

Before committing: `make test`, plus `npm test` and `npm run build` in `frontend/` when the page
changed. The pre-commit hook runs the gate and the secret scan and has no bypass.

---

## Never commit

Secrets, `.env`, `settings.json`, `mcp.json`, the avatar GLB, model weights, session logs, or
fixtures holding personal SRS data beyond the sanitised golden files. `make check-secrets` is part
of `make test` for exactly this reason.

---

## Licence and attribution

The code is MIT ([LICENSE](LICENSE)). Two things it depends on are not, and they place
obligations on what you ship:

- **The default avatar is CC BY-NC 4.0** and is fetched at setup rather than vendored. Replace it
  before any commercial use; making your own at Ready Player Me does not lift the restriction.
  See LICENSE section 1.
- **VOICEVOX requires credit wherever the synthesised audio appears,** and the exact credit
  depends on the configured persona's voice. The table is in
  [README.md](README.md#credits-and-licensing).

By contributing, you agree that your contribution is licensed under the repository's MIT licence.
