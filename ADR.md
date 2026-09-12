# atama-AI — Architecture Decision Record

One log, appended to. Each entry says what was decided, why, what it costs, and **what would
reverse it**. A decision here is not up for casual re-litigation during implementation — if you
think one is wrong, propose superseding it (see [Changing a decision](#changing-a-decision)).

Statuses: **Accepted** · **Superseded by ADR-NNN** · **Proposed**

Source of truth for behaviour is [ATAMA-AI_SPEC.md](ATAMA-AI_SPEC.md); this file explains the
reasoning behind it. Build sequencing is in [ROADMAP.md](ROADMAP.md).

| #   | Decision                                                      | Status   |
|-----|---------------------------------------------------------------|----------|
| 001 | Claude CLI subprocess, not the Anthropic API SDK              | Accepted |
| 002 | One persistent subprocess per session, not one per turn       | Accepted |
| 003 | No tools for the tutor; no permission bypass                  | Superseded by ADR-016 |
| 004 | Local STT: faster-whisper `large-v3` @ `int8_float16`         | Accepted |
| 005 | VOICEVOX on CPU in Docker, never on the GPU                   | Accepted |
| 006 | Server-side Silero VAD over raw PCM from an AudioWorklet      | Accepted |
| 007 | `speakAudio` with explicit visemes, never `speakText`         | Accepted |
| 008 | Sentence-level streaming, with fillers as masking only        | Accepted |
| 009 | Vanilla TS + Vite frontend; no React, no state library        | Accepted |
| 010 | SRS sources are optional; Bunpro is fragile by assumption     | Accepted |
| 011 | Student Profile capped at 600 tokens                          | Accepted |
| 012 | Tutor prompt lives in a versioned file, not in code           | Accepted |
| 013 | No database — JSONL files, one user                           | Accepted |
| 014 | ~6 GB of VRAM headroom is reserved, not spent                 | Accepted |
| 015 | No external interface is coded against unverified             | Accepted |
| 016 | Subprocess isolation: `--tools ""`, strict MCP, empty cwd, env allowlist | Accepted |
| 017 | Loopback-only binding for every service                        | Accepted |
| 018 | Self-barge-in is designed out, not tuned out                   | Accepted |
| 019 | Service health is surfaced from real signals, in the UI       | Accepted |
| 020 | Emotion is one tag, sentence-scoped, driving face and voice together | Accepted |
| 021 | WaniKani and Bunpro are read-only, enforced at three layers     | Accepted |
| 022 | Configuration lives in a settings interface; `.env` is an optional override | Accepted |
| 023 | Bunpro MCP server is written in-repo; credential is the Settings→API token only | Accepted |
| 024 | SRS APIs are called only at launch and manual refresh; MCP tools read the snapshot | Accepted |
| 025 | Session topic seed fetched by the orchestrator, never by the tutor | Superseded by ADR-028 |
| 026 | Sensei has a soul file: persona lives in `prompts/soul.md` | Accepted |
| 027 | The brain is a provider behind an interface; Claude CLI is the only implementation | Accepted |
| 028 | The tutor finds its own topic, through a search tool we provide | Accepted (not yet implemented — V0.11) |
| 029 | Sensei's prompt replaces Claude Code's, and is passed as a file | Accepted |

---

## ADR-001 — Claude CLI subprocess, not the Anthropic API SDK

**Status:** Accepted

**Context.** The tutor needs a conversational brain. The obvious engineering choice is the
Anthropic SDK: typed, streaming, no process management, no CLI flag archaeology. But the user
has a Claude subscription and does not want per-token API billing for what is a daily-driver
personal study tool.

**Decision.** The brain is the `claude` CLI running headless (`claude -p`) with
`--input-format stream-json --output-format stream-json`, authenticated by the existing
subscription login or `CLAUDE_CODE_OAUTH_TOKEN`. The Anthropic API/SDK is not a permitted
substitute.

Two consequences follow directly and are non-negotiable:

- **`ANTHROPIC_API_KEY` is stripped from the child environment at spawn.** If it is present it
  silently overrides subscription auth and bills the API — a failure mode with no error message,
  visible only on a bill. There is a unit test for this.
- **`--bare` is not used**, because bare mode does not read `CLAUDE_CODE_OAUTH_TOKEN`.

**Consequences.** We own subprocess lifecycle, line-oriented JSON parsing, crash recovery, and
exposure to CLI flag changes between versions. Mitigated by ADR-015 (verify and pin) and by the
`--resume` restart path in ADR-002.

**Reversed if:** the user moves to API billing deliberately, or the CLI stops offering a stable
headless stream-json mode.

---

## ADR-002 — One persistent subprocess per session, not one per turn

**Status:** Accepted

**Context.** Spawning `claude -p` per turn is simpler: no state, no zombie processes, no resume
logic. It is also fatal here — process startup alone would consume a large fraction of the 1.60 s
budget allocated to the entire Claude stage (spec §10).

**Decision.** Exactly one subprocess per session. The `session_id` from the `init` event is
captured; if the process dies, it is restarted with `--resume <session_id>` so conversation
memory survives. Unknown stdout event types are logged and skipped, never fatal. A per-turn
timeout (default 60 s) sends SIGINT, surfaces an apology line, and resumes.

**Consequences.** Crash recovery, restart, and resume are load-bearing and must be tested
explicitly (ROADMAP subsystem 1) rather than discovered in use. In exchange, turn *N+1* pays no
startup cost.

**Reversed if:** startup cost ever becomes negligible, which would make the stateless design
strictly simpler.

---

## ADR-003 — No tools for the tutor; no permission bypass

**Status:** Superseded by ADR-016 (2026-09-09). The intent stands; the mechanism changed —
`--disallowedTools` left 20 built-in tools in the prompt, and the subprocess was inheriting the
user's global Claude Code configuration. Reasoning below kept as written.

**Context.** Claude Code ships with file and execution tools. A conversation tutor needs none of
them, and every available tool costs system-prompt tokens — which is latency (ADR-011) — while
adding real risk to a process that consumes microphone-transcribed input.

**Decision.** Spawn with
`--disallowedTools "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch"`. Never use
`--dangerously-skip-permissions`. The only tools available are the Bunpro MCP tools (ADR-010),
and even those are rationed to on-request or roughly every 15 minutes.

**Consequences.** The tutor cannot look anything up locally. That is the point. It also means a
prompt-injection-shaped utterance through the mic has nothing to reach for.

**Reversed if:** a genuinely needed capability appears — and then only that one tool is allowed,
with the latency cost measured.

---

## ADR-004 — Local STT: faster-whisper `large-v3` @ `int8_float16`

**Status:** Accepted

**Context.** Cloud STT would be accurate and cheap in VRAM, but adds network round-trip inside a
3.0 s budget and sends the user's voice off the machine every turn. Japanese STT quality matters
more here than for most applications: a garbled transcript makes the tutor respond to something
the student never said.

**Decision.** `faster-whisper` with `large-v3`, `device=cuda`, `compute_type=int8_float16`,
`language="ja"`, `beam_size=5`, `condition_on_previous_text=False`, `vad_filter=False` (we run
Silero ourselves upstream, ADR-006). The model is warmed at startup with a 1 s dummy
transcription so the first real turn is not slow.

**Fallback, pre-authorised:** if `large-v3` @ `int8_float16` exceeds ~4.5 GB on the installed
driver stack, drop to `medium` int8 via config rather than breach the 10 GB cap (ADR-014). The
accuracy trade-off is accepted and documented; the cap is not negotiable.

**Consequences.** ~3.5 GB VRAM and a hard dependency on a working CUDA stack — `make doctor`
checks it. Whisper hallucinates on silence in Japanese (ご視聴ありがとうございました and
friends), so a blocklist plus avg-logprob and no-speech-prob thresholds are required. The
blocklist is a **data file**, not code, so new hallucinations are one line, not a release.

**Reversed if:** the latency or VRAM budget cannot be met locally at acceptable accuracy — and
that is a conversation with the user, not a unilateral swap (spec §14).

---

## ADR-005 — VOICEVOX on CPU in Docker, never on the GPU

**Status:** Accepted

**Context.** VOICEVOX has GPU builds, and moving TTS to the GPU looks like free latency. It is
not free: it competes with Whisper for VRAM inside a hard 8–10 GB cap, and the CPU build already
fits inside the 0.40 s first-chunk budget for the short sentences this tutor produces (≤ 25
characters, spec §6).

**Decision.** Official VOICEVOX Docker image, CPU build, on `:50021`, managed by
docker-compose — the only containerised component. Never moved to the GPU. Speaker id,
`speedScale` (default `0.9` for learners) and `intonationScale` are configurable in `.env`.

**Consequences.** The GPU is Whisper's alone, and VRAM accounting stays simple. Docker becomes a
prerequisite, which `make doctor` checks.

**Reversed if:** CPU synthesis measurably misses the 0.40 s budget on the target machine — and
even then, only with the VRAM cap re-verified first.

---

## ADR-006 — Server-side Silero VAD over raw PCM from an AudioWorklet

**Status:** Accepted

**Context.** The browser could do voice detection and ship encoded audio (MediaRecorder/opus),
which is less bandwidth and less server work. But opus framing costs decode time and precision
in exactly the stage with the tightest budget (0.50 s for end-of-speech detection), and
end-of-turn detection is a policy decision the orchestrator needs to own, since it also drives
barge-in.

**Decision.** Mic capture via **AudioWorklet at 16 kHz mono PCM16**, streamed raw over the
WebSocket. Silero VAD runs server-side: end-of-utterance = configurable silence (default 600 ms)
after speech ≥ 300 ms. The client keeps a crude local energy check purely to trigger barge-in
fast, confirmed by the server.

**Consequences.** More bytes over a localhost socket — irrelevant. The server owns turn-taking
policy in one place, which is what makes barge-in coherent.

**Reversed if:** the app ever runs over a real network, where bandwidth would start to matter.

---

## ADR-007 — `speakAudio` with explicit visemes, never `speakText`

**Status:** Accepted

**Context.** TalkingHead can lip-sync from text, but it has **no Japanese lip-sync module**.
Meanwhile VOICEVOX already returns exact mora timings in its `audio_query` response — real
phoneme timing, free.

**Decision.** Always `speakAudio(...)` with `{audio, visemes, vtimes, vdurations}` computed from
the VOICEVOX mora timeline. `speakText` is never called. The mora→Oculus-viseme map (spec §7) is
**one pure function** with golden tests against three committed real `audio_query` fixtures.
`prePhonemeLength` is honoured and all durations are divided by `speedScale`.

**Consequences.** Lip-sync quality is bounded by VOICEVOX's timing accuracy, which is good.
`lipsyncModules` can be left empty on init. The timing unit (**ms vs s**) must be verified
against the TalkingHead README and pinned — getting it wrong produces silent drift rather than
an error, which is why it is its own verification spike (ROADMAP V0.4).

**Reversed if:** TalkingHead ships a Japanese text lip-sync module that beats mora timings —
unlikely, since it would have less information.

---

## ADR-008 — Sentence-level streaming, with fillers as masking only

**Status:** Accepted

**Context.** Waiting for a complete reply before synthesis would blow the latency budget on its
own. But splitting too aggressively produces chunks that sound wrong spoken alone.

**Decision.** Cut Claude's stream at `。！？…\n` **as tokens arrive** and ship each sentence the
instant it closes. Synthesis of sentence *N* overlaps generation of *N+1*. If the first sentence
has not closed by 1.2 s, play a pre-synthesised filler (うーん、そうですね…) from a small pool.

**The filler rule:** fillers are **masking, not budget compliance**. True first-content latency
is logged separately from perceived latency, and only the true number is measured against the
p90 gate (≤ 3.0 s when this was written; ≤ 5.0 s since ADR-033). A design that hits the budget
only with fillers has not hit the budget.

**Consequences.** The chunker must never split mid-sentence across delta boundaries — it gets
property tests. The pipeline is concurrent rather than sequential, which is more complex and is
the reason per-stage timings are logged from M2 onward.

**Reversed if:** first-token latency ever drops far enough that whole-reply synthesis fits the
budget.

---

## ADR-009 — Vanilla TS + Vite frontend; no React, no state library

**Status:** Accepted

**Context.** The frontend is one page: a canvas, a subtitle strip, a mic indicator, a settings
drawer. React would add a build layer, a render model, and a dependency tree in exchange for
component ergonomics this UI does not need — and three.js/TalkingHead already own the render
loop, which is exactly where React's model fits worst.

**Decision.** Vite + vanilla TS. A few modules: `main.ts`, `avatar.ts`, `ws.ts`, `mic.ts`,
`ui.ts`. No state library. WS message types are defined once as shared constants and mirrored in
pydantic models, with a contract test asserting the two sets match so drift fails CI rather than
silently no-op'ing in the browser.

**Consequences.** No component reuse machinery. Fine at this size; revisit only if the UI grows
past one page.

**Reversed if:** the UI genuinely grows past one page — and that would first need a conversation
about whether it should (ADR-013's spirit).

---

## ADR-010 — SRS sources are optional; Bunpro is fragile by assumption

**Status:** Accepted

**Context.** WaniKani has a stable official API. Bunpro does not — integration is unofficial and
can break without warning. Personalisation is the whole value proposition, but a broken
third-party integration must never mean a broken tutor.

**Decision.** Both sources are **optional**, fetched in parallel at session start inside a 10 s
budget. The app runs correctly with zero, one, or both configured.

- **WaniKani** — official v2 API, ~60 req/min respected, cached to disk with a 1 h TTL.
- **Bunpro** — treated as fragile: every call wrapped, any failure logs a warning and continues.
  Exposed two ways: a session-start fetch for the static profile, and an MCP server so Claude can
  check the live review queue mid-conversation — rationed to on-request or roughly every
  15 minutes, because each tool call is latency (ADR-011).

`mcp.json` is generated from `mcp.json.template` plus `.env` at startup so credentials are never
committed.

**Consequences.** More error-handling code than a happy path needs, and a degradation matrix in
the standing regression suite. In exchange, Bunpro breaking is a logged warning rather than an
outage.

**Reversed if:** Bunpro ships an official, stable API.

---

## ADR-011 — Student Profile capped at 600 tokens

**Status:** Accepted

**Context.** More context makes a better-informed tutor. It also makes a slower one, and the
Claude stage is the only variable stage in a 3.0 s budget (spec §10). Everything WaniKani and
Bunpro can return would be many thousands of tokens.

**Decision.** The rendered Student Profile is capped at **≤ 600 tokens** — a hard assertion in
the profile renderer's tests, not a guideline. It carries: WaniKani level, per-stage counts,
~30 recent unlocks, ~15 leeches, Bunpro JLPT progress, ~15 recent/ghost grammar points. It is
also written to `logs/profile-<date>.json` for debugging.

This sits alongside the other latency allies, all mandatory: extended thinking off, tools
stripped (ADR-003), MCP use rationed (ADR-010), `--model` exposed so the user can drop to a
faster model if p90 drifts.

**Consequences.** Selection matters — the profile carries the *most useful* items, not all of
them. The human test is whether it would help a human tutor.

**Reversed if:** the latency budget changes, or prompt caching makes profile size cost nothing.

---

## ADR-012 — Tutor prompt lives in a versioned file, not in code

**Status:** Accepted

**Context.** The tutor's personality, correction policy and voice-output rules will be tuned
constantly — that is the actual product work here, and it is not software engineering.

**Decision.** The prompt lives in `prompts/tutor.md`, rendered with `{{student_profile}}` at
session start and passed via `--append-system-prompt`. It is never hardcoded in Python.

**Consequences.** The user iterates on Sensei without touching code or restarting a mental model
of the pipeline. The prompt is version-controlled, so a personality regression is a diff.

**Reversed if:** never, realistically.

---

## ADR-013 — No database — JSONL files, one user

**Status:** Accepted

**Context.** Session history, SRS caches and turn logs all look like things a database stores.
This is a single-user local application on one laptop.

**Decision.** No database, no auth system, no multi-user. `logs/session-<timestamp>.jsonl`, one
clean record per turn — user transcript, assistant text, stage timings, emotion, tool calls —
plus `logs/profile-<date>.json` and a disk cache for WaniKani.

The log is explicitly designed as **the user's future Anki mine**, so schema stability matters
more than richness. A trivial reader script must be able to parse a whole session.

**Consequences.** No queries beyond `grep` and a reader script. Correct at this scale.

**Reversed if:** the log outgrows files — which would mean this became a different product.

---

## ADR-014 — ~6 GB of VRAM headroom is reserved, not spent

**Status:** Accepted

**Context.** The target machine has 16 GB. The measured pipeline uses ~5.6 GB (Whisper ~3.5,
Silero < 0.1, CUDA context ~1, browser/three.js ~1). The remaining headroom is an obvious place
to put a bigger model or a second one.

**Decision.** Hard cap of **8–10 GB** total. The ~6 GB remainder is **deliberately reserved** for
a future MuseTalk/photoreal experiment and is not to be spent on the current pipeline. Anything
that pushes past the cap gets reduced (ADR-004's `medium` int8 fallback) rather than accommodated.
`make doctor` and the `--profile` overlay report `nvidia-smi` and warn above 10 GB.

**Consequences.** Some quality is left on the table today to keep a future option open. That is
the intended trade.

**Reversed if:** the photoreal experiment is abandoned — then the headroom is genuinely free.

---

## ADR-015 — No external interface is coded against unverified

**Status:** Accepted

**Context.** This project depends on four interfaces that drift or are under-documented: the
`claude` CLI's headless flags, VOICEVOX's `audio_query` schema, TalkingHead's `speakAudio`
signature and timing units, and the WaniKani API. Guessing any of them produces failures that are
either silent (wrong timing unit → drift) or misleading (a renamed flag → an unhelpful CLI error).

**Decision.** Before integration code is written, each interface gets a verification spike
(ROADMAP V0.1–V0.6). Findings are pinned as a **dated code comment plus a constant**, and real
responses are committed as fixtures where they are useful for tests. Never invent a flag, a
schema field, or a method signature.

Specifically verified: the exact spelling of `--include-partial-messages`, whether `--verbose` is
required with stream-json output in print mode, the current `--disallowedTools` syntax, the
`audio_query` response shape, and whether `vtimes`/`vdurations` are milliseconds or seconds.

**Consequences.** M1 starts a little later and everything after it is built on facts. When a flag
changes upstream, the dated comment says when it was last true.

**Reversed if:** never.

---

## ADR-016 — Subprocess isolation: `--tools ""`, strict MCP, empty cwd, env allowlist

**Status:** Accepted (2026-09-09) — supersedes ADR-003

**Context.** Verified against Claude Code 2.1.159 on 2026-09-09. Three findings changed the
spawn design:

1. With the spec's `--disallowedTools "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch"`, the
   `init` event still listed **20 built-in tools** (Task, Skill, TodoWrite, ToolSearch, Workflow,
   cron tools, …). Each is prompt tokens — latency — and an unintended capability surface.
   `--tools ""` ("Use "" to disable all tools") exists and removes the built-in set entirely.
2. Non-bare mode performs CLAUDE.md auto-discovery from the working directory and, without
   `--strict-mcp-config`, loads every MCP server in the user's personal Claude Code config.
   Spawned from the repo root, Sensei would be given this repository's `CLAUDE.md` as
   instructions and whatever MCP servers the developer uses day to day.
3. Stripping only `ANTHROPIC_API_KEY` from the child env still passes `WANIKANI_TOKEN` and Bunpro
   credentials into the claude process and every MCP server it spawns.

Also verified: `--session-id <uuid>` lets the orchestrator assign the id at spawn; `init` carries
`apiKeySource`, which is `"none"` under subscription auth; a `rate_limit_event` exists; `result`
carries `ttft_ms` and `duration_api_ms`.

**Decision.** Spawn with `--tools ""`, `--strict-mcp-config`, `--session-id <ours>`,
`--fallback-model`, from an empty dedicated `cwd` (`.cache/claude-cwd/`, stable across restarts
because session persistence is keyed by cwd), with an **allowlisted** environment: `PATH`,
`HOME`-equivalents, temp dir, `LANG`, and `CLAUDE_CODE_OAUTH_TOKEN` if set. Nothing else.
Credentials reach an MCP server only via the `env` block of its `mcp.json` entry. Assert
`init.apiKeySource == "none"` and refuse to run otherwise. Verify at M1 that MCP tools survive
`--tools ""`; if not, fall back to `--disallowedTools` with the full 20-tool list.

**Consequences.** The tutor's prompt is as small as the CLI allows; its only tools are the ones
we hand it. Crash-resume is trivial. API billing is impossible by construction, not by hope. The
cost is a stricter spawn procedure with its own tests (env allowlist, `apiKeySource`, cwd).

**Reversed if:** the CLI changes these flags — which is exactly why they are pinned with a date
in `backend/constants.py` and re-verified on every CLI upgrade.

**Addendum (2026-09-09, live verification).** Two corrections from running the real spawn:
(1) `--tools ""` **does** keep MCP tools — but only if the MCP server is connected before the
first turn; Claude Code prints `init` with `mcp_servers: pending` and emits no connection event,
so our server announces readiness itself (`notifications/initialized` → `ATAMA_MCP_READY`
marker) and the orchestrator waits on that signal, never on a timer. The `--disallowedTools`
fallback is retired: built-in tool names vary by platform and context. (2) The "empty cwd"
must be **outside the repository** — Claude Code discovers CLAUDE.md up the tree and keys
project memory by the detected root; `config.claude_cwd()` defaults to a per-user state dir.

---

## ADR-017 — Loopback-only binding for every service

**Status:** Accepted (2026-09-09)

**Context.** The WebSocket carries the raw microphone stream and the student's SRS profile.
uvicorn's and Docker's convenient defaults (`0.0.0.0`, `50021:50021`) expose both to whatever
network the laptop is on — a café, a hotel.

**Decision.** uvicorn binds `127.0.0.1` (config `HOST`, loopback default), Vite stays on
localhost, and `docker-compose.yml` publishes VOICEVOX as `127.0.0.1:50021:50021`. `make doctor`
fails if any of the three answers on a non-loopback interface. WSL2 port forwarding makes
`localhost` work from the Windows browser without opening anything (spec §15).

**Consequences.** None for the single-user case. Anyone who wants LAN access has to change a
config value on purpose and read the comment next to it.

**Reversed if:** never, for this product. A multi-device version would be a different product
(ADR-013).

---

## ADR-018 — Self-barge-in is designed out, not tuned out

**Status:** Accepted (2026-09-09)

**Context.** Barge-in (spec §2 step 9) fires when VAD detects speech while the avatar is
speaking. On laptop speakers the microphone hears the avatar. Without countermeasures the tutor
interrupts itself on every turn — the first bug M3 would have hit, and one that a single
threshold tweak cannot fix robustly because room acoustics vary.

**Decision.** Three independent layers, all required (spec §8): browser-side
`echoCancellation: true` on `getUserMedia`; a raised VAD threshold plus a 250 ms sustained-speech
requirement while `speaking` (`BARGEIN_THRESHOLD_FACTOR`, default 2.0×); and a 150 ms ignore
window at playback onset. The state-gated threshold lives in the orchestrator, which already owns
turn-taking (ADR-006). Acceptance includes a 10-turn conversation on laptop speakers with zero
self-interruptions, separately from the headphones run.

**Consequences.** Genuine barge-in during playback needs to be slightly louder and slightly
longer than speech during silence. Acceptable — a human interrupting someone does the same.

**Addendum (2026-09-09, implementation).** "2.0x the listening threshold" cannot be applied as a
multiplication: Silero returns a **probability**, capped at 1.0, so 0.5 x 2.0 = 1.0 is unreachable
and would have disabled barge-in entirely — silently, since nothing errors when a threshold is
never crossed. A test caught it. The factor now divides the remaining headroom to certainty
instead, which preserves the intent ("be N times more demanding") and always lands below 1:
0.5 -> 0.75 at factor 2, 0.833 at factor 3. The three layers are otherwise unchanged.

**Reversed if:** a proper AEC stage in the orchestrator ever makes the state-gated threshold
unnecessary.

---

## ADR-019 — Service health is surfaced from real signals, in the UI

**Status:** Accepted (2026-09-09)

**Context.** The spec made WaniKani and Bunpro optional and Bunpro fragile (ADR-010), which
means "the tutor is not using my reviews" has many possible causes: no token, expired token,
cache being served stale, MCP server failed to start, MCP tool never called. Without a visible
signal, the user cannot tell a working session from a silently degraded one, and neither can a
bug report.

**Decision.** A `service_status` WS message per service — `wanikani`, `bunpro`, `bunpro_mcp`,
`claude`, `voicevox`, `stt` — with a small, explicit state set each (spec §5b), sent on change
and every 30 s, rendered as chips in a status bar. States come from **real signals only**: the
fetchers and their cache timestamps; the `init.mcp_servers[]` entry and every subsequent
`tool_use`/`tool_result` pair for the MCP; `rate_limit_event` and fallback activation for
Claude; `GET /version` for VOICEVOX. `stale` is a first-class state. `control: resync` forces a
re-fetch. `make doctor` prints the same table, and the session log header records it.

**Consequences.** A status registry module (`backend/status.py`) and a contract on what each
subsystem reports. Errors shown to the user must be sanitised — a chip must never display a token
or a tokenised URL; the redaction test covers it.

**Reversed if:** never — this is diagnosability, and it gets more valuable as the system ages.

---

## ADR-020 — Emotion is one tag, sentence-scoped, driving face and voice together

**Status:** Accepted (2026-09-09)

**Context.** The original spec had one optional emotion tag per turn, stripped before TTS and
forwarded to the frontend as a mood. That left three gaps: the voice did not change at all, so
the face and the audio disagreed; a multi-sentence turn could not shift tone; and the mood was
applied when the tag was parsed — a full TTS latency before the matching audio started.

**Decision.** The tag set stays `[happy] [thinking] [surprised] [serious]` (plus implicit
neutral) — Claude is told to choose it for how the sentence should *sound*. A tag may open the
turn or any later sentence; the chunker strips it per sentence and it applies until the next
tag or end of turn. It drives **both** outputs from one config table:

- **Voice:** VOICEVOX style id + `speedScale` / `pitchScale` / `intonationScale` per emotion
  (`backend/emotions.py`), with real style ids enumerated from `GET /speakers` at startup and a
  documented fallback to base style + scalars.
- **Face:** TalkingHead mood where one matches (`happy`), and direct ARKit blendshape overrides
  where none does (`surprised`, `serious`, `thinking`), with 300 ms crossfades. Exact API names
  pinned in ROADMAP V0.4, not assumed.

The `speak` message carries the sentence's `emotion`, and the frontend applies it **when that
audio starts playing**, so face and voice change together.

**Consequences.** One more pure table with tests; one more thing to tune by ear in M3. The
acceptance test is explicit: a scripted four-sentence turn in which each emotion is visibly and
audibly distinct.

**Reversed if:** VOICEVOX styles prove too coarse for the tone shifts wanted — then the scalar
overrides carry the load and the style column is dropped, not the mechanism.

---

## ADR-021 — WaniKani and Bunpro are read-only, enforced at three layers

**Status:** Accepted (2026-09-09) — user directive, not negotiable

**Context.** The user's SRS accounts hold years of study state. WaniKani's API can start
assignments, submit reviews and edit study materials; Bunpro's unofficial surface can do the
equivalent. A tutor that could — through a bug, a misread tool description, or a
prompt-injection-shaped utterance from the microphone — submit a review or start a lesson would
corrupt that state in a way that is hard to notice and impossible to undo. The user has
explicitly forbidden any write.

**Decision.** The application **never writes** to WaniKani or Bunpro. Enforcement is
structural, at three independent layers (spec §5):

1. **Token scope** — the WaniKani token is created with no write permissions; `make doctor`
   reads `/v2/user` and warns if any write permission is present. (Bunpro's key is unscoped, so
   the next two layers are its entire defence.)
2. **Client construction** — one shared SRS HTTP client exposing only `get()`. No write method
   exists to be called, and a test asserts it.
3. **Tool surface** — the Bunpro MCP server exposes read tools only. A community server with
   write tools is disqualified unless they can be removed from the surface; `--allowedTools`
   with an explicit read-only MCP tool list is the belt-and-braces. A prompt instruction is
   **not** an enforcement layer.

A standing-suite test records every outgoing HTTP request in a full mocked session, MCP tool
calls included, and asserts all are `GET`.

**Addendum (2026-09-09) — verified at every compilation.** The user elevated this to the
spec's Golden Rule (§0) with mechanical enforcement at every build/start point, not only in the
test suite: a static AST gate (`backend/tools/readonly_gate.py`) that is the first target of
`make test` and a prerequisite of `make run` and `make doctor`; an import-time self-check in
`srs/http.py` that its client exposes only `get`; a `ReadOnlyTransport` that raises on any
non-`GET` request at runtime; a startup assertion on the MCP tool surface; a `prebuild` grep in
the frontend; and a pre-commit hook installed by `make hooks`. The gate bans **any setter-shaped
name** in `backend/srs/**` (`set_`, `write_`, `update_`, `create_`, `delete_`, `submit_`,
`start_`, `post_`, `put_`, `patch_`, `mark_`, `reset_`, `assign_`) regardless of behaviour, and
has no bypass flag, env var, or marker comment. Editing the gate itself requires a new ADR
entry — which, per this decision, will not be written.

**Consequences.** Some features are permanently off the table: the tutor cannot mark a leech as
reviewed, cannot add a study note, cannot start the next lesson for you. If those are ever
wanted they are a *different product* with its own consent step — not a flag in this one.

**Reversed if:** never. This is a user directive and stays until the user reverses it in
writing, in this file.

---

## ADR-022 — Configuration lives in a settings interface; `.env` is an optional override

**Status:** Accepted (2026-09-09) — user directive

**Context.** The original spec put every secret and tunable in `.env`. That is the developer's
convention, not the user's: it means editing a hidden dotfile to change a voice speed or paste a
token, and it gives no feedback about whether a value actually works. The user has said they do
not want a `.env` at all, ultimately — they want a settings interface.

**Decision.** Configuration is owned by a **settings page** in the frontend and stored in
`settings.json` (repo root, git-ignored, mode `0600`, atomic writes). `config.py` resolves
defaults → `settings.json` → environment variables, so env vars remain available as an
*optional* override for automation and CI, and a normal user never creates a `.env`. The page
is generated from the same schema `config.py` reads — every key, grouped, typed, with default
and description — so the three inventories (`config.py`, `.env.example`, settings-page schema)
are asserted identical by a test. Secrets use masked inputs and are echoed back to the browser
only as `{set, hint}`; the full value never leaves the backend after storage. Each external
service has a **Test** button that runs the real check and reports through the status registry
(ADR-019), so "configured" and "working" are one screen. First run with no `settings.json`
opens on the settings page and unlocks the conversation once `claude` tests green.

Secrets in a `0600` JSON file are the same trust class as secrets in `.env` on a single-user
laptop; the OS keyring is a possible later upgrade, not a requirement (ADR-013's spirit — don't
gold-plate).

**Consequences.** A settings schema module, a persisted store, a secrets-masking rule at the
WS boundary, and a settings page that lands in M5 (the M3 drawer is its first slice: runtime
keys only). `.env.example` stays as the key inventory and the env-override reference. The
redaction test (spec §11) extends to `settings` echoes.

**Reversed if:** never for the interface. The storage backend (JSON file vs keyring) may change
without touching this decision.

---

## ADR-023 — Bunpro MCP server is written in-repo; credential is the Settings→API token only

**Status:** Accepted (2026-09-09) — closes ROADMAP V0.7

**Context.** Bunpro has no official API: staff deprecated it in 2024 and removed the docs, and
have since permitted reverse-engineering of the site's `/api/frontend/*` endpoints with an
explicit "may change without warning". Three community MCP servers were surveyed
(ROADMAP findings log, 2026-09-09):

- **PatVandyke/bunpro-mcp** — ships `add_to_reviews`, `remove_from_reviews`, `add_bookmark`,
  `remove_bookmark`. Write tools on the surface: disqualified outright by the Golden Rule.
- **jbeshir/mcp-servers/bunpro** — read-only, but obtains its token by POSTing the user's
  **email and password** to `/users/sign_in`. This application will not hold a password for a
  service it only reads from.
- **yash-278/bunpro-mcp** — read-only, uses the Settings→API token. Acceptable in principle,
  but its eight tools are heatmap/stats-shaped (none returns ghost reviews or per-grammar-point
  SRS state, which is the tutor's actual need), and it is built for hosted HTTP deployment with
  code we would carry for nothing.

Two credential schemes exist for the same endpoints: the **Settings→API "Account API Token"**
(`Authorization: Token token=…`), and a browser cookie JWT (`Authorization: Bearer …`) with a
days-to-weeks lifetime that must be scraped from DevTools or obtained by logging in.

**Decision.**
1. **Write `backend/srs/bunpro_mcp.py`** — a stdio MCP server with exactly three read tools
   (`get_review_queue`, `get_ghost_reviews`, `get_grammar_progress`), built on the GET-only
   `srs/http.py` client, so it is covered by the read-only gate like every other SRS module.
2. **The only Bunpro credential is the Settings→API token.** No email, no password, no cookie
   scraping, no login POST — the login flow of the Go server is precisely the kind of non-GET
   request the gate forbids, and there is no reason to hold a password for read access.
3. **Fail loudly on drift.** Every response is validated against a pinned fixture; a schema
   change surfaces as `bunpro_mcp = failed` with a sanitised reason, never as silently wrong
   data in the tutor's prompt.
4. **Politeness:** ≥ 1 s between requests, no aggressive retry on 429. (2 s, as one surveyed
   server uses, times the five session-start calls exceeds the 10 s budget — measured in tests
   2026-09-09; 1 s matches the other surveyed project's ≤ 1 req/s.)
5. **The token can only go to Bunpro.** Origins are hardcoded constants in `srs/http.py` with
   no override, and the transport enforces a host allowlist and does not follow redirects
   (spec §0 rule 7). The audit of the surveyed servers found nothing malicious, but one of them
   takes its login URL from an env var — the exact shape of bug that would ship a credential to
   the wrong host. We do not have that shape.

**Consequences.** ~200 lines we own instead of a dependency we would have to audit on every
update anyway. The tool surface is exactly what the tutor needs and nothing else. The base URL
(`bunpro.jp` vs `api.bunpro.jp`) and the exact response shapes are pinned live at M1, not from
the survey.

**Reversed if:** Bunpro ships an official, documented, scoped API — then a read-only scoped
token replaces the full-account one and the endpoints move; the tool surface stays.

**Addendum (2026-09-09, live).** The Settings→API token is honoured only with the query
parameter `dangerously_authenticate_using_api_token=true` plus `Origin`/`Referer` of
`https://bunpro.jp`; without it every endpoint returns 401 `AUTH_USER_DENIED`. Pinned in
`backend/constants.py`. Under ADR-024 the MCP server no longer holds this token at all.

---

## ADR-024 — SRS APIs are called only at launch and manual refresh; MCP tools read the snapshot

**Status:** Accepted (2026-09-09) — user directive

**Context.** The first design had the Bunpro MCP server call Bunpro live on every tool
invocation, and the session-start fetch re-ran whenever a 1 h cache expired. Bunpro's API is
unofficial, unscoped, and staff-tolerated; WaniKani's is rate limited. The user's instruction:
never spam those endpoints — check at app launch or on a manual refresh, store the result, and
show its status.

**Decision.**
1. **Two triggers only:** app launch and the student's manual Refresh (`control: resync`).
   No timer, no per-turn fetch, no MCP-driven fetch. A launch re-uses a snapshot younger than
   `SRS_CACHE_TTL_S` (default 10 min) so rapid restarts during development do not hammer the APIs.
2. **Snapshot + status are stored.** `.cache/srs/<service>.json` carries `fetched_at` and the
   raw payloads; `.cache/srs/status.json` persists the registry so the UI shows the last known
   state on restart. Chips read `synced HH:MM`; `stale` means "serving an older snapshot".
3. **The Bunpro MCP server is a snapshot reader.** Its three tools never touch the network;
   every answer includes `synced_at` and `age_minutes` so the tutor can qualify what it says.
   A test asserts the module contains no HTTP client and no token reference.
4. **Therefore no credential enters the MCP process.** `mcp.json` carries only the snapshot
   path. The token lives in the orchestrator's config and is used only at launch/refresh.

**Consequences.** Mid-conversation Bunpro data can be up to a session old — acceptable for a
tutor that is told the age and can suggest a Refresh. The credential surface shrank to one
process. Measured: launch fetch of both sources 4.6 s (budget 10 s).

**Reversed if:** never for the trigger policy (user directive). The snapshot format may change
freely.

---

## ADR-025 — Session topic seed fetched by the orchestrator, never by the tutor

**Status:** Superseded by ADR-028 (2026-09-09, same day, before any code shipped). The user's
judgement: a pre-fetched list of headlines injected into the prompt is rigid, and the tutor
picking its own subject is what makes it a conversation rather than a reading. The
prompt-injection and sanitisation reasoning below carries over to ADR-028 unchanged. Original
reasoning kept as written.

**Context.** The user wants each session to open on a real subject — news from Japan and the
wider world — discussed naturally while weaving in their recent WaniKani vocabulary and the
grammar they are currently studying. Without a seed, a voice tutor opens with 「今日はどうですか」
every time and the conversation dies.

The obvious implementation is to give Sensei a web-search tool. That is wrong three times over:
it breaches `--tools ""` (ADR-016), it adds tool-definition tokens to every turn's prompt
(ADR-011), and it puts a network round-trip *inside* the 3.0 s voice→voice budget (§10).

**Decision.** The **orchestrator** fetches headlines at app launch and on manual Refresh — the
same two triggers as SRS (ADR-024) — sanitises them, and renders them into the system prompt as
a seed. The tutor keeps zero web tools and pays zero per-turn cost. Several headlines are
offered and the tutor picks one, so sessions vary without another fetch.

Source chain, each optional and degrading to the next:
1. **SearxNG** (`SEARXNG_URL`) — the user's suggestion and a good fit for a local-first app.
   **Not implemented:** no instance was available to verify against, and ADR-015 forbids coding
   against an unverified interface. The config key and ROADMAP V0.11 hold the place.
2. **NHK RSS** — verified live 2026-09-09, six categories including `cat6` 国際 (world news).
   Japanese-language throughout, so world events arrive already in the target language: no
   translation step, and the headline itself is study material.
3. **`backend/data/topics.txt`** — evergreen subjects, always available, works offline.

NHK **News Web Easy** (やさしい日本語) would have been ideal for a learner and is unusable: its
`news-list.json` now redirects to `news.web.nhk` and returns `401 missing_token` (JWT). Verified,
not assumed.

**Prompt-injection surface.** Headlines are third-party text entering a system prompt. Only
titles are used — never article bodies — stripped of control characters and of `[`/`]` (which
would collide with the emotion tags of ADR-020), length-capped, with the feed's own channel
title discarded. The tutor has no tools, so the worst outcome is an odd remark, not an action.

**Consequences.** One more optional network dependency at launch, inside the existing fetch
budget, with its own status chip. The topic is as fresh as the last sync — correct for a tutor
that is told the age of its data.

**Reversed if:** never for the "orchestrator fetches, tutor does not" split. Sources may change
freely.

---

## ADR-026 — Sensei has a soul file: persona lives in `prompts/soul.md`

**Status:** Accepted (2026-09-09) — user directive

**Context.** The user wants "background and life" for the teacher. A tutor with a consistent
history — where she is from, what she does on Sundays, what she finds funny — has something to
say when the student stalls, and gives the conversation somewhere to go. A model told only
"you are a tutor" produces a customer-service voice.

**Decision.** A second versioned, user-editable prompt file, `prompts/soul.md`, rendered into
`{{soul}}` at the **top** of the system prompt. Same rule as ADR-012: never hardcoded in Python,
so the user shapes Sensei's character without touching code. Optional — absent, she is a neutral
competent tutor.

Two constraints keep it from doing damage:
- **Position.** Persona comes first, the HARD OUTPUT RULES last, so the voice-pipeline
  constraints win by position; the soul file itself states that it may colour *how* she speaks
  and never override those rules.
- **Budget.** Capped at 400 tokens by the same estimator as the profile (ADR-011), asserted in
  tests. Backstory is exactly the sort of thing that grows until it silently eats the latency
  budget.

**Consequences.** Three prompt inputs to assemble (`soul`, `student_profile`, `topic`) instead
of one. A renderer with a token budget per section, and a test that the assembled prompt stays
within its total.

**Reversed if:** never. This is a user directive and the cost is one file.

---

## ADR-027 — The brain is a provider behind an interface; Claude CLI is the only implementation

**Status:** Accepted (2026-09-09) — user directive. Refines ADR-001, does not supersede it.

**Context.** ADR-001 fixes the brain as the `claude` CLI subprocess for a good reason
(subscription auth, no per-token billing). But the rest of the pipeline — VAD, STT, chunker,
TTS, visemes, avatar — has nothing to do with *which* model produces the text. The user wants
the option to swap in a local model or another vendor later without that being a rewrite. Raised
before `claude_session.py` was written, when the seam costs nothing; adding it afterwards would
mean unpicking Claude-shaped types from the orchestrator, the status registry and the logs.

**Decision.** Introduce a narrow **`Brain`** interface. Everything upstream of the chunker talks
to it and never imports a provider module.

```
start()                 -> ready to take turns
send(text)              -> async iterator of BrainEvent
aclose()
```

`BrainEvent` is deliberately provider-neutral: `TextDelta`, `ToolCall`, `ToolOutcome`,
`RateLimited`, `BrainError`, `TurnComplete(text, ttft_ms, duration_ms, usage)`. Nothing in it
names Claude, MCP, or a CLI flag.

Three things the interface refuses to assume, because they are where providers actually differ:

1. **Conversation memory is the provider's problem.** The Claude CLI keeps history itself
   (`--session-id` + `--resume`); an OpenAI or llama.cpp provider would keep a transcript and
   resend it. The orchestrator only ever sends the latest user turn.
2. **Tool access is not MCP.** The three Bunpro tools are plain Python functions over a local
   snapshot (ADR-024) — no network, no credentials. `backend/srs/bunpro_tools.py` holds the
   implementations; the MCP server is a thin adapter over them *for Claude*, and a provider with
   native function-calling would bind the same functions directly.
3. **Health is reported as `brain`, not `claude`.** The status chip carries the provider name in
   its detail (`claude-cli · sonnet`). `apiKeySource` and `rate_limit_event` are Claude-specific
   signals mapped onto the neutral states of §5b.

**Only `ClaudeCliBrain` is implemented, and ADR-001 still governs it** — subscription auth,
`--tools ""`, allowlisted env, out-of-repo cwd, `--resume` on crash. No second provider is
written until someone actually wants one (ADR-013: do not gold-plate). The interface is the
deliverable; speculative adapters are not.

**Consequences.** One extra indirection and a small event-type vocabulary. In exchange, swapping
brains touches one directory. It also makes the session testable without a subprocess: tests
drive a `FakeBrain` over the same interface, which is how the chunker, timing and status paths
get covered without spawning a CLI.

**Reversed if:** never realistically — the cost is an interface file.

---

## ADR-028 — The tutor finds its own topic, through a search tool we provide

**Status:** Accepted (2026-09-09) — user directive. Supersedes ADR-025.

**Amended 2026-09-10 (user directive: "the search for news can come from multiple sources,
including Yahoo News").** Still one read tool behind SearxNG, with three changes, all verified live:
results are interleaved round-robin by provider (one engine was supplying 82% of them); Yahoo!
JAPAN's official topic RSS is merged into `news` results as one provider, with a 72-hour age
cutoff; and two dead default SearxNG engines are disabled. NHK's RSS is excluded because it is
frozen — it answers 200 with month-old items. See spec §5c for the measurements.

**Context.** ADR-025 had the orchestrator fetch headlines at launch and paste them into the
prompt. The user rejected it: a fixed list is rigid, it cannot follow the conversation, and it
makes the opening a recitation. If Sensei can *look things up*, she can pick something worth
talking about, follow the student's interest into it, and check a fact when the conversation
actually needs one.

**Decision.** Give the tutor a **search tool** backed by **SearxNG** — self-hosted metasearch,
no API key, no third-party account, which keeps the local-first property intact. It is added to
`docker-compose.yml` next to VOICEVOX so one `docker compose up -d` brings up everything.

Boundaries, which is where the real design is:

- **A separate MCP server** (`backend/search_mcp.py`), never a fourth tool on the Bunpro server.
  The Bunpro server's three-tool surface is asserted by the Golden Rule gate (spec §0 rule 5);
  that assertion stays exactly as it is.
- **Read-only and rationed by the prompt**: search at the start of a session to find something
  to talk about, and afterwards only when the conversation genuinely needs a fact — not every
  turn. Same discipline as the Bunpro tools (ADR-010).
- **The latency cost lands where it is affordable.** A search happens on the *opening* turn,
  before the student has said anything, where a couple of seconds is invisible. The 3.0 s
  voice→voice budget (§10) governs conversational turns; a turn in which Sensei chooses to
  search is logged with its tool time broken out so the p90 measurement stays honest.
- **Results are untrusted text** (ADR-025's reasoning, retained): titles and short snippets
  only, control characters and `[`/`]` stripped (they would collide with the emotion tags of
  ADR-020), length-capped, count-capped. The tutor still has no built-in tools (ADR-016), so a
  hostile result can make her say something odd and nothing more.
- **Optional, degrading cleanly.** No SearxNG reachable → the tool reports that, the chip reads
  `down`, and Sensei opens from the student's profile and last session instead. The app never
  blocks on it. There is no static topic list; the fallback is her own memory and curiosity.

**Not yet implemented — ADR-015 applies.** SearxNG's JSON API (`/search?format=json`) has not
been verified against a running instance: none was reachable and the Docker daemon was down.
The compose service and the config key exist; the client is written only once a live instance
answers (ROADMAP **V0.11**).

**Consequences.** A fourth tool in the tutor's prompt and one more optional service. Sessions
start differently every time, which is the point. Search results reach the model unmediated, so
the sanitiser is load-bearing.

**Reversed if:** searching proves too slow even on the opening turn, or the user would rather
not run another container — in which case ADR-025's pre-fetch returns as the cheap alternative.

---

## ADR-029 — Sensei's prompt replaces Claude Code's, and is passed as a file

**Status:** Accepted (2026-09-09). Refines spec §4, which originally specified
`--append-system-prompt "$(cat …)"`.

**Context.** The spec assumed appending our tutor prompt to Claude Code's default one. Running it
showed three separate problems, all measured on 2026-09-09 against CLI 2.1.159:

1. **Append leaves a coding agent in front of the tutor.** Asked to introduce herself, Sensei
   said 「私はClaude Codeです。Anthropicが開発したAIアシスタントで、ソフトウェアエンジニアリング
   のタスクを支援します」 and offered to debug code, in markdown bullets — every HARD OUTPUT RULE
   broken. `init.tools` also came back empty. With the prompt *replaced* she is みなみ先生, speaks
   in short sentences with emotion tags, and the three MCP tools are present.
2. **The string flags truncate at the first newline.** A three-line prompt given to
   `--system-prompt` reached the model as line one only: it ignored its own name on line 2 and an
   explicit instruction on line 3. The same text joined onto one line was applied in full.
3. **They also swallow every following flag.** Our prompt contains lines starting with `-`, so a
   multi-line value made the CLI lose the `--mcp-config` that came after it. The tutor started
   with no tools, silently. Nothing logged an error; it looked exactly like a broken MCP server,
   and cost an afternoon of bisection.

**Decision.** Pass the rendered prompt with **`--system-prompt-file`** (replace). The file
variants — `--system-prompt-file`, `--append-system-prompt-file` — exist but are documented only
inside the `--bare` help text, which is why the first pass concluded they did not. Append remains
available via `CLAUDE_REPLACE_SYSTEM_PROMPT=false` for anyone who wants Claude Code's default
behaviour back. `--mcp-config` is still ordered before the prompt flag, defensively.

**Consequences.** The tutor loses Claude Code's built-in system prompt entirely — correct here,
since none of it is about teaching Japanese, and it removes ~7 000 tokens of irrelevant
instructions from every turn. Our prompt is now the *whole* prompt, so `prompts/tutor.md` carries
full responsibility for behaviour. Passing a file also keeps the student profile out of `ps`,
which the earlier argv approach could not.

**Reversed if:** a future CLI fixes multi-line handling *and* someone wants the default agent
behaviour back — neither of which changes the persona argument for replacing.

## ADR-030 — A tutor is a persona *and* a voice; voices are shortlisted by measurement and chosen by ear

**Status:** Accepted (2026-09-09). Extends ADR-026 (soul file), refines spec §7.

**Context.** ADR-026 gave Sensei a persona file. It did not say who picks the voice, and the first
implementation kept them in two settings: `TUTOR_PERSONA` chose the character, `VOICEVOX_SPEAKER`
chose the voice. They drifted immediately — a male persona ran in a female voice, which is jarring
in a way that no amount of good prompt text repairs.

Worse, the voice had been picked by browsing names. The user then reported that たなか "sounds
unstable between phonemes", which was correct and which no test caught, because nothing in the
repo measured a voice at all. Choosing by name is choosing at random.

**Decision — three parts.**

1. **The persona declares its voice.** `prompts/<name>.md` carries `<!-- voice: NN -->` on its own
   line; `VOICEVOX_SPEAKER=-1` (the default) means "ask the persona". The declaration is stripped
   before the file reaches the model — it is configuration, not character. `VOICEVOX_SPEAKER` set
   to a real id still overrides, for auditioning.

2. **Voices are shortlisted by measurement, never by name.** Before a voice may be declared in a
   persona file, it is measured across the whole `GET /speakers` catalogue on one fixed sentence:
   - **F0 jitter**, mean frame-to-frame |ΔF0| over mean F0, autocorrelation tracker at a 5 ms hop,
     reported in **cents** so registers compare;
   - **spectral flux**, mean L2 change of the normalised magnitude spectrum — the "rough between
     phonemes" axis;
   - **shimmer**, frame-to-frame RMS change over mean RMS;
   - **synthesis time per sentence**, because it is charged against the §10 budget.
   The numbers produce a shortlist. They do **not** produce the answer.

3. **The human picks from the shortlist by ear.** Measurement is necessary and not sufficient: it
   ranks stability, and stability is not the same as suitability. たなか is the proof — he runs on
   the *least* steady voice in the catalogue (麒ヶ島宗麟, 33.4 cents against the best male voice's
   19.9) because on a fifty-year-old ex-engineer that unsteadiness reads as age. A gate that
   maximised the metric would have thrown that away.

**The catalogue** (`prompts/`, each file declaring its own voice):

| persona | who | voice | jitter | synth |
|---|---|---|---|---|
| `tanaka` | 50, male, ex-engineer, dry, explains by example | 麒ヶ島宗麟 53 | 33.4 cents | ~615 ms |
| `hayashi` | 28, male, fast, current usage over textbook order | 栗田まろん 67 | 22.0 cents | ~643 ms |
| `minami` | 40s, female, linguistics, warm, literary | No.7 29 | 20.2 cents | ~874 ms |
| `mori` | 19, female, **not a teacher** — a 語学交換 conversation partner | 冥鳴ひまり 14 | 15.3 cents | ~933 ms |

`mori` exists because the range that mattered turned out to be *role*, not only age: a partner who
lets you finish a wrong sentence is a different tool from a teacher who corrects it, and the young
voice is what makes that role credible. The other three are teachers.

**Consequences.**

- Adding a tutor is adding one file. No Python changes, no config changes.
- `SINGLE_STYLE_SPREAD` was split into `SINGLE_STYLE_PITCH_SPREAD` (1.8) and
  `SINGLE_STYLE_INTONATION_SPREAD` (1.0, i.e. none). Widening `intonationScale` stretches the
  model's own F0 wobble along with the contour — measured 2.15% → 2.41% jitter at the old 1.8× —
  so single-style speakers now buy emotional range with pitch offset only, which costs nothing.
- Synthesis cost is now a **persona-level** property, from ~615 ms to ~1010 ms per sentence across
  the voices auditioned. The §10 voice→voice p90 must be measured against the *configured*
  persona, not against a single pinned number.
- A voice cannot be swapped casually: it needs the sweep re-run and a listening pass.

**What this does NOT decide.** Whether VOICEVOX itself is the right engine. The sweep showed the
top four male voices within 2 cents of each other and the top female voices within 2 — i.e. the
residual synthetic quality the user still hears is the vocoder, not the speaker, and no choice
inside this catalogue addresses it. Replacing the engine is ADR-005's business and would need its
own verification spike; see ROADMAP V0.3.

**Reversed if:** the engine changes (a new engine's voices need their own sweep), or personas ever
need to share one voice — in which case the declaration moves back out to config.

---

## ADR-031 — Memory is read once at session start and written in the gaps; never retrieved mid-turn

**Status:** Accepted (2026-09-09); **amended 2026-09-10** — a recent-topics tier, and summarising moves to the next launch (see *Amendment* below); **amended 2026-09-12** — a fifth tier, and memory split per tutor (see *Amendment 2*). Extends ADR-024's principle to a second kind of expensive work.
See spec §6b.

**Context.** Spec §6 and ADR-028 already tell Sensei to open "from what she knows about them or
from last session" — but nothing stored a last session, so that instruction had nothing behind it.
A tutor who forgets you between lessons is not a tutor; asking 「先週の旅行はどうでしたか」 is most
of what makes the persona worth having.

The obvious implementation is the wrong one. A retrieval step — a vector store, or a memory tool
the model calls when it feels like it — puts a lookup between the student finishing a sentence and
the first audio coming back. The §10 budget for that whole path is 3.0 s and the first sentence
already costs 1.8-2.4 s. A 300 ms retrieval is a large slice of the remaining headroom, spent at
the one moment the student is waiting. Worse, it is *variable*, and in a voice conversation an
unpredictable pause reads as the tutor not having understood you.

**Decision.** Memory never touches the critical path. Three tiers, each with one read moment and
one write moment:

| tier | file | read | written |
|---|---|---|---|
| turn log | `logs/sessions/<date>-<session>.jsonl` | never by the tutor | appended in the gap after each turn |
| student notes | `<state>/memory/student.md` | session start, into the prompt | session end |
| last-session brief | `<state>/memory/last-session.md` | session start, into the prompt | session end |

1. **Reads happen once, at session start**, and become part of the system prompt beside the soul
   and the SRS profile — same machinery, same token budgeting, a new `MEMORY_MAX_TOKENS` section
   truncated at a line boundary and reported (ADR-011, `prompt.py`). After that the model has
   everything it will get. There is no memory tool, and there will not be one.

2. **Writes happen in the dead air.** When `TurnComplete` fires, the avatar still has seconds of
   synthesised audio to play and the orchestrator is idle. That gap is where the turn record is
   appended. Nothing is written while the student is speaking or while a turn is in flight.

3. **Summarising happens at session end**, as a separate short-lived `Brain` call — not by the
   tutor mid-conversation, which would spend a turn and pollute the transcript. Its input is the
   turn log, so it is deterministic and re-runnable; if the app is killed before the summary is
   written, the next launch rebuilds it from the log it finds.

4. **Best-effort, always.** Every memory operation is cancellable and none may block a turn. If the
   student speaks while a write is in flight, the conversation wins and the write is abandoned. A
   lost turn record is a small loss; a hesitation is the product being bad.

5. **Human-editable, and outside the repo.** `student.md` is markdown the user can open and fix,
   like the soul file (ADR-026). A wrong memory confidently recalled is worse than no memory, and
   the only practical correction mechanism is a text editor. It lives in the per-user state
   directory, is gitignored, and is never committed: it holds the student's life, not the
   project's.

**What goes in `student.md`** — pedagogically shaped, not a transcript: grammar points missed more
than once, vocabulary the student produced *unprompted* (evidence they own it, rather than that
they were shown it), topics that got them talking, and facts about their life the tutor should not
have to be told twice. Explicitly not: full transcripts, and not anything already in the SRS
profile — WaniKani and Bunpro are the authority on what is being studied (ADR-024), and duplicating
it would let the two disagree.

**Consequences.**

- Cross-session recall costs **zero** milliseconds per turn. It is prompt tokens, which are cached
  (`cache_read_input_tokens` is non-zero on every turn after the first), not latency.
- The tutor cannot look something up mid-conversation. If it was not loaded at start, it is not
  known. Accepted: a tutor who says 「あれ、なんだっけ」 is more human than one that stalls.
- The turn log stops being only a repo convention. It is also the Anki mine, so its schema is a
  stable contract from its first commit.
- Session end acquires a job that can fail. It fails silently into "no brief next time".

**Rejected: a vector store / RAG.** ADR-013 (no database), plus the latency argument above. The
corpus is one student's lessons — small enough that the interesting parts fit in a prompt section,
which makes retrieval machinery pure cost.

**Amendment (2026-09-10, user directive: "a small database of the latest conversation topics, light
on tokens, so we can catch up and not always have the same conversation").** Two changes, neither
of which touches the rule that memory stays off the critical path:

1. **A fourth tier, `<state>/memory/topics.jsonl`** — one line per summarised session holding at
   most five short noun phrases. The last eight sessions are rendered as a single *recently
   discussed — do not open on these* line. This is what the original three tiers could not do: the
   brief says what happened *last* time, but nothing stopped the tutor opening on the same news
   item three sessions running. It is still a file, not a database (ADR-013), and still read once.
2. **Summarising moves to the next launch.** Point 3 above made the next launch the fallback for a
   killed app; it is now the normal path. Exit must be instant, and a summariser on exit is fifteen
   seconds of a Ctrl+C that appears to hang. Launch is init time the student already waits
   through for Whisper. The summariser runs on `MEMORY_SUMMARY_MODEL` (default `haiku`) over a
   capped text-only excerpt — once per session, never per turn — which keeps the token cost of
   memory to that one short call plus a few hundred cached prompt tokens.

Token budget: the memory section is capped at `MEMORY_MAX_TOKENS = 220` and `TOTAL_MAX_TOKENS`
moves 2100 → 2350, enforced by a worst-case test with soul, profile and memory all maxed.

**Rejected: letting the tutor write memory through a tool.** It spends a turn, it happens at a
moment the model chooses rather than one we control, and it puts a write on the critical path —
the exact thing this ADR exists to prevent.

**Reversed if:** a student accumulates so much history that `student.md` cannot be summarised into
its budget without losing things that matter. The next step then is scoping by topic at session
start — still a start-of-session read, still not per-turn retrieval.

---

**Amendment 2 (2026-09-12, user request) — they know each other, and each tutor knows them
separately.**

*Context.* The brief and the topics made the lessons continuous, but not the relationship: the
tutor did not know the student's name, where they lived, or whether they had already talked about
the tutor's cat. The user asked for a memory that is "not a really long one" but enough that the
two feel like they have met — and then, seeing it, that it be per tutor.

*Decision.* A fifth tier, two short hand-editable lists, read like the others at session start and
written by the same summariser: durable facts about the student (10) and about the tutor's own
claimed life (6). One line each, dedup on the letters so a rewording is not learned twice, and
when the list is full the OLDEST survive with the newest few always given a slot — a name is
learned in lesson one and must outlive a month of small talk. Tutor facts are written without
pronouns: the tutor is a man or a woman depending on the voice the student chose.

*And the drawer is per tutor.* `<state>/memory/<tutor>/` holds the brief, the topics and that
tutor's facts; `student.md` and `about-me.md` stay shared, because the student is the same person
whoever teaches them. The turn log records who taught each lesson so only that tutor summarises
it, and switching `TUTOR_PERSONA` — live, from the settings panel — switches the memory in place.

*Cost.* `MEMORY_MAX_TOKENS` 220 → 400 and the total 2950 → 3150 (§10 latency). A tutor who forgets
your name every week is not worth the tokens it saves.

*Rejected:* one shared memory for the whole cast (a new tutor recalling a lesson they were not at
is worse than one who asks); a facts *tool* the model calls mid-turn (ADR-031's whole point);
unbounded facts (a diary that grows is a prompt that grows).

## ADR-032 — Context is rotated pre-emptively during the avatar's speech, never compacted mid-turn

**Status:** Accepted (2026-09-09). See spec §6b. Depends on ADR-031's turn log. Amended
2026-09-11: the compaction point is watched for, not measured (below).

**Context.** A lesson is a long conversation. The Claude CLI keeps the transcript itself (ADR-027,
§4) and compacts it when the window fills — automatically, at a moment of its choosing, taking as
long as it takes. In a chat client that is a progress spinner. In a voice conversation it is the
tutor going silent for several seconds mid-lesson, with no explanation, and no way for the student
to tell whether they should repeat themselves. It is the worst latency event this design can
produce, and it arrives precisely when the conversation has been going well long enough to fill a
window.

We can see it coming. Every `TurnComplete` carries the provider's `usage`, and for this CLI that is
`{input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens, ...}` —
verified against a real turn, 2026-09-09. The first three sum to what the model actually read that
turn, which tracks live context size for free, on a field we already parse.

**Decision.** Track context growth per turn and **rotate the session before the provider decides to
compact**, in a gap where nobody is waiting:

1. Sum `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` on each
   `TurnComplete`. Cost: an addition.
2. Above `CONTEXT_ROTATE_AT` — a fraction of the model's usable window; **the window size and the
   provider's own compaction trigger must be measured, not assumed (ROADMAP V0.12)** — arm a
   rotation.
3. Perform it in the **speaking gap**: the turn is done, the avatar has seconds of audio left.
   Build a handoff brief from the turn log (ADR-031) — deterministic, no model call, nothing to
   wait on — then spawn a second process with a fresh `--session-id` and a system prompt of the
   usual sections plus that brief.
4. **Swap at a turn boundary, never inside one.** If the new process is not ready when the student
   speaks, keep the old one and try again in the next gap. Rotation is never the reason a turn is
   slow.
5. If rotation keeps failing, let the provider compact and **log it as a latency event**, so it
   appears in the §10 instrumentation as what it is rather than as a mysterious slow turn.

**Also budgeted: what the model re-reads every turn.** Tool results are the fastest way to fill a
window — a search result set or an SRS snapshot pasted whole is thousands of tokens that will be
re-read on every subsequent turn for the rest of the session. Tool output is summarised to its own
budget before it enters context. That is cheaper than rotating more often.

**Consequences.**

- The student never experiences a compaction stall. At worst they experience a tutor who has
  forgotten the exact wording of something from forty minutes ago — which is what a person does.
- Two `claude` processes exist briefly. Both are spawned under ADR-016's rules, and the old one is
  closed only after the new one has taken a turn.
- Rotation is a real seam and it will show if done badly: the handoff brief is everything the new
  session knows. It gets the same care and the same token budget as the rest of the prompt.
- `--resume` (crash recovery, §4) and rotation must not fight. A crash during rotation resumes
  whichever session is still authoritative — the old one, until the swap completes.

**Rejected: raising the threshold and hoping.** The failure is not rare; it is guaranteed in a long
lesson, and it gets more likely the better the lesson is going.

**Rejected: rotating on a turn count or a clock.** Neither tracks what actually fills the window.
One long tool result can do more than twenty turns of conversation.

**Amendment 2026-09-11 — the compaction point is watched for, not measured.** Point 2 asked for
the provider's trigger to be measured once (ROADMAP V0.12). The user rejected that: it is the
provider's policy and can change under us. Verified the same day (CLI 2.1.159): no command reports
it — `/context` gives usage and window only, and the documented `autoCompactWindow` and
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` did not trigger a compaction at 24.5k tokens — but the CLI
announces every compaction on the stream with its trigger, sizes and duration. So the threshold is
a fraction of the window the CLI reports each turn (default 0.7). Each compaction becomes a brain
event, explained on screen and recorded on its turn. An automatic one that arrives before our
threshold lowers it to 85 % of where it happened, kept across launches. Also verified: `/compact`
can be sent in `-p` — the "told when to do it" path below — but it stalled 11.9 s for 24.5k tokens
and writes Claude Code's coding-session summary, so it does not reverse this decision.

**Reversed if:** the provider gains a way to compact incrementally, or to be told when to do it.
Then we tell it, in the same gap, and skip the second process.

---

---

## Changing a decision

Per spec §14: when a verification step fails or reality contradicts the spec, **ask the human,
then update the spec** — do not silently diverge.

To change something here:

1. Add a new ADR entry stating what changed and what evidence forced it.
2. Mark the old entry **Superseded by ADR-NNN**. Do not edit its reasoning — the record of what
   was believed, and why, is the point.
3. Update [ATAMA-AI_SPEC.md](ATAMA-AI_SPEC.md) and [README.md](README.md) to match.
4. Then change the code.

In that order.

---

## ADR-033 — Latency gate relaxed to p90 ≤ 5.0 s: answer quality over the last seconds

**Status:** Accepted — user directive, 2026-09-10. Supersedes the 3.0 s figure of spec §10 and
the gate it set (named in ADR-008, ROADMAP M3d).

**Context.** The first 20-turn run of the latency harness (ROADMAP 10,
`backend/tools/latency_run.py`) measured voice→voice **p50 3.49 s, p90 5.30 s** on sonnet at
effort medium. The Claude stage tracks the model's thinking — roughly +0.5 s per 100 characters;
the turns with none ran 1.7–2.6 s end to end. Thinking cannot be switched off on Sonnet 5: the
toggle, `alwaysThinkingEnabled` and `MAX_THINKING_TOKENS=0` have no effect there (constants.py,
docs read 2026-09-10). Every route under 3.0 s — lower effort, a smaller model, another provider
— trades away answer quality: the corrections and explanations that are the point of a tutor.

**Decision.** The user chose quality: "5 s instead of 3 s for a high-quality answer is good."
The hard gate becomes **voice→voice p90 ≤ 5.0 s over 20 turns** (M3d). The stage budgets keep
their shape; the Claude first-sentence budget grows from 1.60 s to 3.60 s, thinking included.
Effort stays `medium`, the model stays Sonnet. It is still a hard gate measured by the harness,
not an aspiration, and fillers still do not count toward it (ADR-008). `LATENCY_WARN_S` 5.0.

**Consequences.** Latency work now targets what costs no quality: STT and TTS (each slightly
over their stage budgets, partly from sharing the GPU), prompt size, the length of her first
sentence, and perceived latency (a visible thinking cue; fillers as masking). The baseline is
0.30 s over the new gate.

**Amended the same day (user directive):** no latency change that could touch quality — the
user declined asking for a shorter first sentence, a smaller Whisper beam, and fillers. Latency
work is limited to what provably cannot change a word she says or hears (measurement, visible
thinking cues). A clean re-run then measured p90 6.03 s: **M3d is not met at these settings**,
mostly from server-side time-to-first-token variance (one turn with no thinking at all waited
11.4 s). Whether the gate is raised, made non-blocking, or accepted as failing is the user's
call when M4 is reached.

**Stage budgets, same day (user):** asked about the two stages that also miss their own budgets
at these settings — STT p90 0.58 s against 0.35 s, first synthesised audio p90 0.59 s against
0.40 s — the user answered "that stage is fine". Both are accepted at their measured values:
M2b and M2c are judged on their non-latency checks (no hallucinations on silence; N synthesises
while N+1 generates; five emotions audibly distinct), and their timing is reported, not gating.

**Reversed if:** 4–5 s pauses turn out to break real lessons, or a model or provider keeps the
quality at a lower thinking cost — then measure with the harness and decide again.

---

## ADR-034 — M2 declared done by the user; M4 is built before M3

**Status:** Accepted — user directive, 2026-09-10: "declare M2 done, build M4 then M3". Amends
the strict milestone order of spec §12 and CLAUDE.md for these two milestones only.

**Context.** M2's code has been in daily use (voice loop, push-to-talk, device recovery, the
start screen, live tutor switching). Of its gates: **M2d met** (0.0 ms at every emotion speed);
**M2b and M2c timing accepted** at their measured values (ADR-033). **Not formally measured**:
M2a (no false end-of-turn in 3 minutes of hands-free speech — push-to-talk is the default and
hands-free tuning was deferred by the user), M2b's 2-minute silence hallucination check, M2c's
overlap evidence from the timing log, and M2c's five-emotion listening test. VRAM
instrumentation and rolling p50/p90, due "M2 onward", now exist.

**Decision.** M2 is done by the user's declaration, with those four checks **deferred to the
end of the project** (user, same day: "we will do M2 at the end, there is no blocker there") —
not met until they are run. M4 (memory and rotation, status indicators from real signals, resync,
prompt tuning) is built next, then M3 (the Vite/TypeScript frontend, ADR-009).

**Consequences.** During M4 the avatar page is still the prototype (frontend/public/preview.html);
M4's UI pieces — status chips, the resync button — land there and are ported in M3. The deferred
checks run at the end of the project; a failure there reopens that subsystem, not the milestone.

**Reversed if:** a deferred check fails in real use badly enough that M3 or M4 work is built on
a broken base.

---

## ADR-035 — The page's protocol is generated; barge-in closes a turn epoch; a face changes with its audio

**Status:** Accepted (2026-09-11), built at M3. Refines ADR-009 (the contract test) and ADR-020
(when the face changes).

**Context.** Building the M3 page found three things the prototype got wrong, each silently:

1. ADR-009 asked for a test that the page's message *names* match the pydantic models. Names are
   the least of it: a renamed field, a new literal or a changed type drifts just as silently, and
   a hand-kept mirror drifts by construction.
2. Barge-in never reached the page. The orchestrator stopped its own queue, but never sent
   `bargein`, and every `speak` carried `turn: 0` — the page could not tell a sentence of the
   interrupted turn from one of the next, so she played on to the end of what she had.
3. The emotion was applied when a `speak` *arrived*. TalkingHead queues sentences, so the face ran
   a whole sentence ahead of the voice — exactly what ADR-020 forbids.

**Decision.**

1. `frontend/src/protocol.gen.ts` is **generated** from `backend/models.py` — every type, field and
   literal (`python -m backend.tools.gen_protocol`). `test_models.py` fails while the committed
   copy is stale; on the page, the dispatcher's `Handlers` type needs one handler per server type,
   so `npm run build` fails on a missing one.
2. The orchestrator keeps a **turn epoch** (`Hub.epoch`): up by one when a turn starts and when she
   is interrupted. `state` and `speak` carry it; `bargein` carries the epoch it closes. The page
   drops any sentence of a closed epoch however late it arrives, even one still decoding. Under
   push-to-talk the page stops her **on the key event itself** — a press while she talks is
   unambiguous — and the server's `bargein` confirms; the page logs key-to-silence per barge-in,
   which is the M3b measurement.
3. The face is set by **TalkingHead's subtitle callback**, which fires when a sentence leaves its
   queue and its audio starts (talkinghead.mjs 1.4, read 2026-09-11) — one word spanning the
   sentence at t = 0, so it fires once.

**Consequences.** Changing a message means running the generator; forgetting fails the tests. One
field was added to the frozen M2 protocol: `state.turn`. The rig panel's samples play outside any
epoch. Browser-side capture (ADR-006's AudioWorklet) is still not built — the orchestrator owns
the microphone — so the constraints-object test of ROADMAP 8 does not apply yet.

**Reversed if:** a second client appears in another language; then generate a JSON Schema and
derive each client from it instead of emitting TypeScript directly.

---

## ADR-036 — The study panel: the tutor tags, the student clicks, the dictionary is local

**Status:** Accepted by the user (2026-09-11). Partly built 2026-09-12: the chat beside the tutor,
the tutor's inline marks, red grammar, the hint (point 1), and furigana from local data — the
tokenizer and the student's WaniKani kanji progress (point 3's readings, point 4's `FURIGANA` and
`STUDY_PANEL`). Explanations and translations on click (point 2) followed on 2026-09-13:
`backend/explain.py`, one cached answer per grammar point and language, and per sentence. Word
cards (the rest of point 3) are not built yet. Spec §8b.

**Context.** User request: the tutor on the left, the conversation on the right like a messaging
app; every important grammar point in red, clickable for its rule in Japanese or English (a
setting); vocabulary with on'yomi, kun'yomi and English; a translate icon at the end of each of
her sentences; and a hint telling the student which form or word she is waiting for them to use.
Built naively — a model annotating every sentence — that is one more LLM call per sentence on a
subscription whose limit is a five-hour window. The user asked for the smart way.

**Decision** (each option chosen by the user):

1. **The tutor tags inline.** She already picks each grammar point on purpose — she weaves in the
   student's Bunpro ghost reviews — so she marks it as she writes: `{{span|point}}` around the
   phrase, `[target:point]` for what she wants the student to produce next, and `[used:word]` for
   something the student has just used correctly, which the page floats behind her (user,
   2026-09-12) — she is the only one who can judge that. A few output tokens
   a turn, no extra call, no added latency. The chunker strips both before TTS and before the
   subtitle text, exactly like emotion tags (ADR-020), and sends the spans with the sentence.
   **Red is grammar only** (user, 2026-09-12, after a lesson in which a noun went red and 〜てみよう
   did not): the prompt now defines what a grammar point is and lists the conjugations most often
   missed, and `Annotator.grammar_only` drops a mark whose every token is a noun unless the point
   is named as a pattern (〜…, as Bunpro writes it). The guard is deliberately narrow — it reuses
   the tokenizer already loaded for furigana, costs nothing, and a missed drop is a stray red word
   while an over-eager one would hide a real point.
2. **Explanations and translations only on click**, from a one-shot side call: `claude -p` on the
   haiku tier (the CLI, never the API — ADR-001), no tools, under §4's isolation rules, off the
   critical path, and **cached on disk** per grammar point and language, and per sentence. Each is
   paid for once, and only if someone asks.
3. **Vocabulary is local, with zero LLM cost**: the student's WaniKani data first (already cached,
   read-only — no new calls, ADR-021/024), then an offline dictionary (KANJIDIC2 / JMdict), with a
   Japanese tokenizer for word boundaries and furigana. These are new pinned dependencies; the
   packages and their APIs are verified before any code uses them (ADR-015).
4. Settings: `EXPLAIN_LANGUAGE` (`en` | `ja`), `FURIGANA`, `STUDY_PANEL`.

**Consequences.** Tag use becomes a prompt-tuning target measured like gate M3f; a sentence
without a tag simply shows plain text. The tutor's prompt grows by the tag rules, inside the
existing budget. The first click on something costs a small call; the second is free. The
dictionary data is downloaded at setup and credited in the README (JMdict and KANJIDIC2 are
CC BY-SA).

**Rejected:** a background annotation call every turn (the user was offered it; it pays for what
is never opened); translations written by the tutor into her reply (English is output tokens and
seconds before her voice, for text most turns never need); vocabulary from the model (a
dictionary's readings are exact and free).

**Reversed if:** inline tagging proves unreliable in prompt tuning; then one background call per
turn replaces point 1.
