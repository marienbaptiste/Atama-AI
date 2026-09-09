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
p90 ≤ 3.0 s gate. A design that hits the budget only with fillers has not hit the budget.

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
