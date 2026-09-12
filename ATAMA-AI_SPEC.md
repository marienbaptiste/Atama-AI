# PROJECT: atama-AI (頭AI) — Real-time Voice Japanese Tutor with 3D Avatar

You are building a complete, runnable, local-first application. Read this ENTIRE document before writing any code. Then present a plan, wait for approval, and build milestone by milestone. Do not skip milestones. Do not invent APIs — when unsure about an external interface (claude CLI flags, VOICEVOX endpoints, TalkingHead methods, WaniKani API), verify against `claude --help`, the service's live endpoint, or the library's README before coding against it.

---

## 0. GOLDEN RULE — NEVER SET OR WRITE THROUGH THE WANIKANI OR BUNPRO API KEYS

**This application reads from WaniKani and Bunpro. It never sets, writes, creates, updates, submits, starts, or deletes anything through either API key. Not from the orchestrator, not from a fetcher, not from the MCP server, not from Claude, not from a test, not from a script, not "temporarily".**

This is verified mechanically **at every compilation** — meaning every point where the code is built or started:

| When | What runs | Fails the build/start if |
|------|-----------|--------------------------|
| `make test` (first target, before any test) | `make check-readonly` → `backend/tools/readonly_gate.py` | any violation below |
| `make run` and `make doctor` (before anything starts) | same gate | same |
| backend **import time** | `backend/srs/http.py` self-check | its client class exposes any public method other than `get` |
| backend **startup** | MCP tool-surface assertion | the Bunpro MCP server's tool list ≠ exactly `{get_review_queue, get_ghost_reviews, get_grammar_progress}` |
| **runtime**, every request | `ReadOnlyTransport` in `srs/http.py` | `request.method != "GET"` → raises `ReadOnlyViolation`, request never leaves the process, logged as CRITICAL, status chip → `error` |
| **runtime**, every request | host allowlist in the same transport | `request.url.host` not in `{api.wanikani.com, api.bunpro.jp, bunpro.jp}` → raises `HostViolation`; the token cannot be sent anywhere else, including via redirect (redirects are not followed) |
| `npm run build` (frontend) | grep gate in `package.json` `prebuild` | any `wanikani.com` / `bunpro.jp` URL or `WANIKANI_TOKEN` / `BUNPRO_API_TOKEN` string in `frontend/src` — the browser never talks to either service |
| `git commit` (recommended pre-commit hook, installed by `make hooks`) | `make check-readonly` + `make check-secrets` | same as the gate |
| CI (if ever added) | `make test` | same |

**What `readonly_gate.py` checks (static AST scan, no network):**
1. In `backend/srs/**` and any module that imports from it: no call to `httpx`/`requests`/`aiohttp`/`urllib` HTTP methods other than `get`; no `.request(` call whose `method` argument is not the literal `"GET"`; no `method=` keyword with any other value.
2. In `backend/srs/**`: no function, method, or attribute whose name starts with `set_`, `write_`, `update_`, `create_`, `delete_`, `submit_`, `start_`, `post_`, `put_`, `patch_`, `mark_`, `reset_`, `assign_` — **no setter of any kind**, regardless of what it does. Rename or remove; there is no allowlist.
3. Only `backend/srs/http.py` may import an HTTP library. Every other SRS module goes through it.
4. `backend/srs/http.py` defines exactly one public client method, `get`, and the `ReadOnlyTransport` guard (the gate pins the class name `_GuardTransport`, kept as an alias of it; a refusal is logged CRITICAL and the service's chip goes to `error`).
5. The MCP server module (if written) registers only the three read tools, and each tool's implementation calls only `get`.
6. No **code** file (`.py`, `.ts`, `.js`, `.json`, `.yaml`, `.toml`, `.sh`, `Makefile`) anywhere in the repo contains the WaniKani write scopes as strings (`assignments:start`, `reviews:create`, `study_materials:create`, `study_materials:update`, `user:update`) **except** the doctor's scope reminder (`backend/tools/doctor.py` prints the scopes to leave unticked; it cannot verify them — V0.9). Documentation may name them in order to tell the user to leave them unticked.
7. **SRS base URLs are constants, never settings.** `srs/http.py` hardcodes the WaniKani and Bunpro origins; no env var, setting, or function argument can change them (the survey of community servers showed a configurable base URL is exactly how a misconfiguration would ship a token to the wrong host). The gate fails on any `http://` / `https://` literal in `backend/srs/**` other than those origins, and on any base-URL parameter.

A violation prints the file, line, and rule, and exits non-zero. There is no `--skip`, no environment variable, no marker comment that bypasses it. If the gate itself is edited, the edit must be accompanied by a new ADR entry — and ADR-021 says that entry will not be written.

Everything in §5 and ADR-021 is the detailed form of this rule. If any other sentence in this document appears to permit a write, this section wins.

---

## 1. WHAT THIS IS

A desktop web app where the user has real-time spoken Japanese conversations with a 3D avatar tutor. The brain is Claude Code running headless (`claude -p`, subscription auth — NOT the API). The tutor knows the user's exact study state from WaniKani and Bunpro and adapts vocabulary/grammar accordingly. TTS is VOICEVOX (standard Tokyo-accent Japanese). The avatar is a Ready Player Me / Avaturn GLB rendered with the TalkingHead library, lip-synced from VOICEVOX mora timings.

Target machine: single laptop with an NVIDIA RTX-generation GPU of 16 GB, Linux or Windows/WSL2. Everything runs locally except Claude inference.

**Two hard, non-negotiable performance requirements (details in §10/§10b):** voice→voice latency ≤ 5.0 s at p90 (relaxed from 3.0 s by the user on 2026-09-10: answer quality over the last seconds, ADR-033), and total GPU memory use within an 8–10 GB budget. Every design decision must be checked against these; both are instrumented and enforced in milestone acceptance criteria.

## 2. ARCHITECTURE (FIXED — do not redesign)

```
Browser (frontend/, Vite + TypeScript)     Python Orchestrator (backend)
┌──────────────────────────────────┐  WS   ┌──────────────────────────────┐
│ left: TalkingHead avatar         │  /ws  │ FastAPI + asyncio  (app.py)  │
│   visemes, emotion at audio start│ :8000 │  ├─ Hub: fan-out, turn epochs│
│ right: chat thread               │◄─────►│  ├─ VoiceLoop (PTT | VAD)    │
│   red grammar · word cards ·     │       │  ├─ VAD (silero)             │
│   translate · hint               │       │  ├─ STT (faster-whisper)     │
│ status bar · settings · SPACE    │       │  ├─ Brain → claude -p        │
└──────────────────────────────────┘       │  ├─ SentenceChunker (+ tags) │
  src/protocol.gen.ts is generated         │  ├─ Annotator: furigana,     │
  from backend/models.py (gate M3a)        │  │   cached explain via      │
                                           │  │   claude -p (haiku)       │
        ┌─────────────┐                    │  ├─ TTS client → VOICEVOX    │
        │ VOICEVOX    │◄──HTTP─────────────┤  ├─ SRS fetcher (WK/Bunpro)  │
        │ (Docker)    │  :50021            │  ├─ Memory/tutor + rotation  │
        └─────────────┘                    │  └─ Status registry          │
        ┌─────────────┐                    └───────────┬──────────────────┘
        │ SearxNG     │◄──HTTP :8888──┐                │ stdin/stdout
        │ (Docker)    │               │                ▼
        └─────────────┘   ┌───────────┴──┐        claude -p (persistent subprocess,
  Yahoo! JAPAN RSS ◄─GET──┤ search MCP   │◄───────stream-json in/out, MCP tools)
  (news_feeds.txt)        │ (1 tool)     │             │
                          └──────────────┘             └──► Bunpro MCP (3 read tools,
                                                            reads the SRS snapshot)
  run.cmd / up.py  : docker up → wait ready → build the page if stale → orchestrator → open page
  stop.cmd / down.py, or the page's stop button (control quit): tutor, server AND containers
              ▲                                        ▲
              │ style id                               │ persona text
        ┌─────┴────────────────────────────────────────┴──────┐
        │ prompts/<persona>.md                                │
        │   <!-- voice: NN -->  ──► VOICEVOX style id         │
        │   persona text        ──► --system-prompt-file      │
        └─────────────────────────────────────────────────────┘
              one file per tutor; TUTOR_PERSONA selects it

        <state>/memory/                     what is true of the student
        ├─ student.md      (how they learn) ──┐ shared by every tutor
        ├─ about-me.md     (who they are)   ──┤
        └─ <tutor>/                           ├──► {{memory}} in the prompt
           ├─ facts.md     (this tutor's own life)      │
           ├─ last-session.md                           │
           └─ topics.jsonl (do not open on these) ──────┘
              switching TUTOR_PERSONA switches the drawer
```

**Where the build stands against this diagram (2026-09-11).** The browser page is the Vite +
TypeScript app in `frontend/` (ADR-009), built into `frontend/dist` by the launcher and served by
`backend/app.py`; it is the only page (the prototype `preview.html` was removed, user, 2026-09-11).
It renders and plays, and sends `control`, `settings` and `explain` messages. The **microphone is
captured by the orchestrator** (`backend/audio.py`, sounddevice, with the device recovery of §9),
never by the browser: the page holds the turn open with push-to-talk `control: start` / `stop`
(`cancel` drops the hold) over the WebSocket, and there is no audio message from client to server
(the protocol in §8 is the whole set). Memory is spec §6b, search §5c, the launcher §15.
The orchestrator's Python is split into `backend/repl.py` (the CLI entry), `backend/orchestrator.py`
(lesson wiring, rotation, resync, persona switch), `backend/page_control.py` (browser control
dispatch) and `backend/terminal.py` (console rendering).

**A tutor is a persona and a voice together, from one file** (ADR-030). `prompts/<name>.md` holds
the character *and* declares the VOICEVOX style it is written for; `TUTOR_PERSONA` picks the file
and both follow. `VOICEVOX_SPEAKER=-1` (default) means "ask the persona"; a real id overrides it
for auditioning. Adding a tutor is adding one file — no Python, no config. Shipped: `tanaka`
(50, m), `hayashi` (28, m), `minami` (40s, f), `mori` (19, f — a conversation partner, not a
teacher).

Ports: orchestrator `:8000`, VOICEVOX `:50021`, frontend dev server `:5173` (or served statically by FastAPI).

**Bind to loopback only.** The WebSocket carries the tutor's audio, the student's transcripts and their study marks. uvicorn binds `127.0.0.1` (config `HOST`, default loopback — never `0.0.0.0`), Vite stays on localhost, and `docker-compose.yml` publishes VOICEVOX as `127.0.0.1:50021:50021` and SearXNG as `127.0.0.1:8888:8080`, never `0.0.0.0`. `make doctor` fails if any of the three is reachable on a non-loopback interface. The handshake also checks `Origin` (§8, ADR-017): only the page's own origin is accepted.

### Data flow per turn
1. The orchestrator captures the mic (16 kHz mono, `backend/audio.py`); the page holds the turn open with push-to-talk `control: start` / `stop` over the WebSocket (`TURN_MODE=ptt`, the default).
2. Releasing the key ends the turn; in `TURN_MODE=vad` Silero VAD detects end of speech instead (`VAD_SILENCE_MS`, default 900 ms).
3. faster-whisper transcribes (language="ja").
4. Transcript written as a user turn to the claude process stdin (stream-json).
5. Claude's streamed text is cut into sentences at 。！？…\n as tokens arrive.
6. Each sentence → VOICEVOX `audio_query` + `synthesis` → WAV + mora timings.
7. Orchestrator converts mora timings → Oculus viseme timeline, sends `{audio, visemes, vtimes, vdurations, text}` to browser.
8. Browser queues it into TalkingHead `speakAudio(...)`; avatar speaks with lip-sync.
9. If the student presses the talk key while the avatar is speaking (or, in `vad` mode, VAD fires) → BARGE-IN: stop playback, flush TTS queue, interrupt the brain's turn (§4), mark remaining assistant text as undelivered, treat new speech as next turn.

## 3. TECH STACK (PINNED)

- Python 3.11+, FastAPI, uvicorn, `websockets`/starlette WS, httpx, pydantic v2
- `faster-whisper` (model `large-v3`, device=cuda, compute_type=`float16`), `silero-vad`
- VOICEVOX engine via official Docker image (CPU build is fine; do not fight for GPU VOICEVOX)
- Frontend: plain Vite + vanilla JS/TS (NO React needed), three.js, `@met4citizen/talkinghead` v1.7+
- Avatar: user-supplied GLB from Avaturn or Ready Player Me (must include ARKit + Oculus viseme blendshapes — RPM exports do by default; document the required export params from TalkingHead README Appendix A)
- Brain: `claude` CLI, headless. Auth = existing subscription login or `CLAUDE_CODE_OAUTH_TOKEN`.
- docker-compose for VOICEVOX; everything else runs bare (whisper needs the host GPU).

## 4. THE CLAUDE SUBPROCESS — DO / DON'T (this is the heart; get it exactly right)

**DO** spawn exactly ONE persistent process per session:
```
claude -p \
  --input-format stream-json \
  --output-format stream-json \
  --include-partial-messages \
  --verbose \
  --model <configurable, default sonnet> \
  --fallback-model <configurable, default haiku> \
  --session-id <uuid generated by the orchestrator> \
  --effort <configurable, default medium> \
  --tools "" \
  --strict-mcp-config \
  --mcp-config .cache/mcp.json \
  --allowedTools "<the MCP tool names>" \
  --system-prompt-file .cache/prompts/<session-id>.txt
```
(one rendered prompt per brain, named for its session id — a rotation's replacement and the
explainer each get their own file)
spawned with **`cwd` = an empty dedicated directory** (`.cache/claude-cwd/`, created at startup) and an **allowlisted environment** (see below).

Flags verified against `claude --help` on 2026-09-09, Claude Code **2.1.159**, and pinned in `backend/constants.py` (re-verify whenever the CLI is upgraded — flags occasionally change):
- `--include-partial-messages` — spelled exactly so; "only works with --print and --output-format=stream-json".
- `--tools ""` — "Use "" to disable all tools" from the built-in set. This replaces the earlier `--disallowedTools` list: with the disallow list alone, **20 built-in tools remained** in the tutor's prompt (Task, Skill, TodoWrite, ToolSearch, Workflow, cron tools…). MCP tools come from `--mcp-config`, not the built-in set — **verify at M1 that Bunpro MCP tools still appear in the `init` event's `tools` list with `--tools ""`**; if they do not, fall back to `--disallowedTools` with the full 20-tool list from the init event.
- `--strict-mcp-config` — "Only use MCP servers from --mcp-config, ignoring all other MCP configurations". Without it the tutor inherits every MCP server from the user's personal Claude Code config: unexpected tools **and** prompt tokens.
- `--session-id <uuid>` — the orchestrator assigns the id at spawn instead of parsing it from `init`; `--resume <that uuid>` after a crash is then trivial.
- `--fallback-model <model>` — automatic fallback "when the default model is overloaded"; also our answer to subscription rate limits (see `rate_limit_event` below).
- `--bare` — confirmed: "Anthropic auth is strictly ANTHROPIC_API_KEY … OAuth and keychain are never read". Never use it.
- `--no-session-persistence` — exists; **never pass it** (kills `--resume`).
- `--system-prompt-file` / `--append-system-prompt-file` **do exist** (documented only inside the `--bare` help text) and are the only correct way to pass the prompt — see the block below.

**The system prompt REPLACES Claude Code's, and is passed as a FILE (verified live 2026-09-09 — this supersedes the `--append-system-prompt` this section originally specified; ADR-029).** Three findings, each measured:

1. **`--append-*` leaves the coding agent in front.** With the tutor prompt appended, Sensei introduced herself as 「私はClaude Codeです。Anthropicが開発したAIアシスタントで、ソフトウェアエンジニアリングのタスクを支援します」, replied in markdown bullets, and `init.tools` came back **empty**. With `--system-prompt-file` (replace) she is みなみ先生, in short spoken sentences, and the three MCP tools are present. Replace is the default; `CLAUDE_REPLACE_SYSTEM_PROMPT=false` selects append.
2. **The string variants truncate at the first newline.** A three-line prompt passed to `--system-prompt` reached the model as line 1 only — it ignored both its own name on line 2 and an explicit test instruction on line 3. The identical text on a single line was applied in full.
3. **…and they swallow every flag that follows.** Our prompt has lines beginning with `-`, so a multi-line value made the CLI lose the `--mcp-config` placed after it: the tutor started with **no tools at all**, with no error anywhere. The symptom is indistinguishable from "MCP is broken", which is what makes it worth writing down.

Using a file fixes all three and keeps the student profile out of `ps`. Order still matters defensively: `--mcp-config` before the prompt flag.

**Why the empty `cwd`, and why it must be OUTSIDE the repository:** non-bare mode does CLAUDE.md auto-discovery from the working directory **and its ancestors**, and keys project memory by the detected project root. Verified live 2026-09-09: spawned from `.cache/claude-cwd/` *inside* the repo, `init.memory_paths.auto` pointed at the Atama-AI project and the model referred to "the atama-AI backend" unprompted. The default is therefore a per-user state directory outside the tree (`%LOCALAPPDATA%\atama-ai\claude-cwd` on Windows, `$XDG_STATE_HOME/atama-ai/claude-cwd` elsewhere — `config.claude_cwd()`), and it must be **stable** across restarts because session persistence is keyed by cwd and `--resume` needs it.

**MCP readiness — wait for the signal, never sleep (verified live 2026-09-09).** Claude Code prints `init` *before* MCP servers connect (`mcp_servers: [{"name":"bunpro","status":"pending"}]`) and emits **no** "connected" event on stdout. If the first user turn is written while the server is pending, the model gets **no tools** and hallucinates. Our MCP server therefore announces readiness itself: on the client's `notifications/initialized` it writes the marker at `ATAMA_MCP_READY` (`{pid, connected_at}`; removed at shutdown; ~1.1 s after spawn). `brain/claude_cli.py` waits on that marker (timeout `CLAUDE_MCP_READY_TIMEOUT_S`, 20 s → status `bunpro_mcp = failed`, conversation continues without the tools) **before writing the first turn**. With the marker honoured, `--tools ""` yields `init.tools == [the three mcp__bunpro__* names]` and `mcp_servers: connected` — **V0.8 answered: `--tools ""` stands.** The `--disallowedTools` fallback is retired: built-in tool names vary by platform and context (a Windows run exposed `PowerShell`, `TaskCreate`… that the first probe never listed), so no static list is reliable.

**Child environment — allowlist, do not merely strip:** the child gets exactly `PATH`, `HOME` (`USERPROFILE`/`APPDATA`/`LOCALAPPDATA` on Windows), `TMPDIR`/`TEMP`/`TMP`, `LANG`, `SYSTEMROOT` and `COMSPEC` (the `claude.CMD` shim is a cmd.exe script and needs both), and `CLAUDE_CODE_OAUTH_TOKEN` if set. Nothing else (`constants.CLAUDE_CHILD_ENV_ALLOWLIST`). This is what keeps `WANIKANI_TOKEN` and Bunpro credentials out of the claude process and out of every MCP server it spawns. (Since the fetch policy of §5, the Bunpro MCP server reads the launch snapshot and needs **no credential at all**; its `mcp.json` `env` block carries only the snapshot path.) `ANTHROPIC_API_KEY` is therefore excluded by construction — and a unit test asserts it stays excluded even when set in the parent.

Verified `init` event fields (2.1.159): `session_id`, `model`, `tools[]`, `mcp_servers[]`, `apiKeySource`, `claude_code_version`, `cwd`, `permissionMode`. Verified `result` fields: `subtype`, `is_error`, `duration_ms`, `duration_api_ms`, `ttft_ms`, `num_turns`, `stop_reason`, `api_error_status`, `usage`, `modelUsage`, `total_cost_usd`, `session_id`. A `rate_limit_event` type exists. Per-entry shape of `mcp_servers[]` is still to be pinned with a real server configured (ROADMAP V0.2).

- **DO** write user turns to stdin as newline-delimited JSON: `{"type":"user","message":{"role":"user","content":[{"type":"text","text":"..."}]}}` — flush after each line.
- **DO** parse stdout line-by-line as JSON events. Handle at minimum: the `init` event (assert `session_id` matches the one we passed — verified 2026-09-12 to hold on `--resume` as well; read `apiKeySource`, `tools`, `mcp_servers`), streamed partial text deltas, complete assistant messages, tool-use events and their `tool_result` blocks (surface as "thinking" state to the UI, and feed the Bunpro MCP status indicator — §5b), `rate_limit_event` (surface in the status bar; the fallback model handles continuation), and the `result` event per turn (log `ttft_ms` and `duration_api_ms` into the turn timing record — they are the Claude stage's ground truth). Log and skip unknown event types — do not crash on them.
- **DO** assert `apiKeySource == "none"` on `init`. Anything else means an API key reached the child and the session is billing the API: refuse to continue, surface the error, and stop.
- **DO** restart with `--resume <session_id>` (same cwd, same env) if the process dies, so conversation memory survives crashes.
- **DO** strip a leading emotion tag — one of the seven in `chunker.EMOTIONS`: `[happy]` `[thinking]` `[surprised]` `[serious]` `[encouraging]` `[proud]` `[confused]` — from assistant text before TTS; the sentence's `speak` message carries it as `emotion` (§8).
- **DON'T** spawn a process per turn (startup latency kills the experience).
- **DON'T** use `--bare` — bare mode does not read `CLAUDE_CODE_OAUTH_TOKEN`.
- **DON'T** pass the parent environment through. Allowlist (above). `ANTHROPIC_API_KEY` silently overrides subscription auth and bills the API; SRS tokens have no business in the child.
- **DON'T** spawn from the repo root or any directory containing a `CLAUDE.md`.
- **DON'T** use `--dangerously-skip-permissions`. The tutor needs no file/exec tools at all; `--tools ""` both removes risk and shrinks the prompt.
- **DON'T** let a wedged turn hang the app: per-turn timeout (default 60 s) → **interrupt** the turn (below), surface an apology line, restart with `--resume` if the interrupt never settles.

**Stopping a turn in flight — the interrupt protocol (verified live 2026-09-12 against CLI 2.1.159, pinned in `constants.py`; ADR-037).** Signals are not the tool. A barge-in or a timed-out turn writes one line to the same stdin: `{"type":"control_request","request_id":"<id>","request":{"subtype":"interrupt"}}` — the Agent SDK's wire protocol, not in `claude --help`, so it is re-verified on every CLI upgrade. The CLI answers with a `control_response`, then the turn's `result` with `subtype: "error_during_execution"`, all within the same millisecond; **the process stays up**, and the next user turn is answered normally on the same session id (preceded by a fresh `init`). A deliberate interrupt that never settles within the grace (`INTERRUPT_GRACE_S`) is closed as a whole tree and the session is resumed **silently**, because the student asked for the interruption. On Windows `shutil.which("claude")` is `claude.CMD`, a cmd.exe shim that runs `claude.exe` as a child: `proc.terminate()` kills the shim only and `claude.exe` survives orphaned (measured), so the one whole-tree stop is `taskkill /PID <launcher> /T /F` (`brain.claude_cli.kill_tree`). The clean exit is stdin EOF.

### 4b. Claude login flow (what the user does, once)

Two supported paths; `make doctor` explains which is active.

1. **Interactive login (default).** Run `claude` once in a terminal **on the machine that will run the backend** (for Windows/WSL2 that means inside WSL2 — credentials live in the WSL home, not the Windows one), then `/login`. The CLI opens a browser or, when it cannot, prints a URL to paste. Credentials are stored under `~/.claude/` outside the repo and refresh automatically. Headless `claude -p` reuses them with no further setup.
2. **Long-lived token.** `claude setup-token` ("Set up a long-lived authentication token (requires Claude subscription)") prints a token; paste it into the settings page as the Claude OAuth token (`CLAUDE_CODE_OAUTH_TOKEN`). Interactive — the user runs it, never `make`. Use this when the backend runs somewhere the interactive login is awkward.

`make doctor` (`backend/tools/doctor.py`, written 2026-09-12; `--skip-claude` skips the probe, `--live` adds one GET per configured SRS token — §5) checks, in order: `claude` on PATH and its version (warn if it differs from the pinned verified version); `ANTHROPIC_API_KEY` **absent** from the shell that runs `make` (hard fail if present — it will leak into everything); a trivial `claude -p "respond with OK" --output-format stream-json`, spawned under the app's own rules (allowlisted env, cwd outside the repo, no shell), whose `init.apiKeySource == "none"` and whose `result.result` is `OK`. That is the only reliable "you are on subscription, not API billing" proof available. Then: VOICEVOX, the app port and SearXNG on loopback and **not** on any other interface; the Docker engine and the two containers; the tokens (set/unset, never the value) and the WaniKani scope reminder; `.gitignore` via `git check-ignore`; the pre-commit hook; CUDA via `nvidia-smi` against `VRAM_WARN_GB`; the persona's avatar file; the §15 topology; and the last persisted §5b status table. Every line is PASS / WARN / FAIL with what to do about it; the exit code is non-zero on any FAIL.

**Subscription rate limits are a real failure mode** for a chatty voice app. On `rate_limit_event`: surface it in the status bar, let `--fallback-model` carry the conversation, and log it. If a turn fails outright with `api_error_status`, speak an apology line and keep the session alive.

## 5. SRS INTEGRATION (WaniKani + Bunpro)

**HARD RULE — READ-ONLY (the detailed form of the Golden Rule, §0). This application never writes to WaniKani or Bunpro.** No starting assignments, no submitting reviews, no creating study materials, no updating user settings, no marking anything. Not from the orchestrator, not from the MCP server, not from Claude. Enforced at three layers, all required:
1. **Token scope.** The WaniKani personal access token is created with **no write permissions checked** (leave `assignments:start`, `reviews:create`, `study_materials:create`, `study_materials:update`, `user:update` unticked). The API does not expose a token's scopes (V0.9, 2026-09-09), so this layer cannot be verified after the fact: `make doctor` prints the scopes to leave unticked, and with `--live` makes **one** GET per configured token to prove it authenticates (only on demand — ADR-024). Bunpro's API is unofficial and unscoped, so layers 2 and 3 are the whole defence there.
2. **Client construction.** The SRS HTTP client (`backend/srs/http.py`, shared by both fetchers and by the Bunpro MCP server) exposes **only** a `get()` method. There is no `post`/`put`/`patch`/`delete` to call by accident, and a test asserts a non-GET method is not reachable through it.
3. **Tool surface.** The Bunpro MCP server exposes read tools only (`get_review_queue`, `get_ghost_reviews`, `get_grammar_progress`). If V0.7 picks a community server that has write tools, it is disqualified unless those tools can be removed from the surface — `--allowedTools` with an explicit read-only MCP tool list is the belt-and-braces; a prompt instruction is **not** sufficient.

A test in the standing suite records every outgoing HTTP request during a full mocked session (including MCP tool calls) and asserts all are `GET` (`backend/tests/test_srs_readonly_session.py`).

At session start (parallel, 10 s total budget, both OPTIONAL — the app must run fine with zero, one, or both configured):
- **WaniKani** (official, stable): `GET https://api.wanikani.com/v2/user` and `GET /v2/assignments?...` with `Authorization: Bearer $WANIKANI_TOKEN`. Extract: level, count of items by SRS stage, every vocabulary item still below Guru (kanji + reading + meaning; the newest thirty were not enough — §8b), ~15 leeches (low-stage, high-incorrect items) if derivable. Respect the ~60 req/min rate limit; store the snapshot to disk (`SRS_CACHE_TTL_S` re-uses a snapshot younger than that at launch; default 0 — every launch fetches, user directive 2026-09-12). Collections are paginated by following `pages.next_url`, up to 20 pages per collection (`wanikani.MAX_PAGES`, more than a level-60 student has); a collection that hits the cap is reported as truncated in the chip's `last_error`, never silently short.
- **Bunpro** (unofficial — treat as fragile): via MCP server configured in `mcp.json` so Claude can query it live mid-conversation, AND a session-start fetch of JLPT progress + ~15 recent/ghost grammar points for the static profile. Wrap every Bunpro call in try/except; on any failure log a warning and continue without it. Never let Bunpro breakage block startup.

**The Bunpro MCP server: we write it (ROADMAP V0.7, decided 2026-09-09; ADR-023).** Bunpro has **no official API** — it was deprecated in 2024 and the docs removed; reverse-engineering the site's `/api/frontend/*` endpoints is permitted by staff with the warning that they "may change without warning". Three community MCP servers exist: one has write tools (disqualified by §0), one needs the user's email + password (disqualified — this app never holds a password), one is read-only but stats-shaped and built for hosted deployment. So: `backend/srs/bunpro_mcp.py`, a small stdio MCP server exposing exactly `get_review_queue`, `get_ghost_reviews`, `get_grammar_progress`, on the same GET-only client as the session-start fetch.

**Fetch policy — never spam the SRS APIs (user directive 2026-09-09, ADR-024).** WaniKani and Bunpro are contacted **only at app launch and on the student's manual Refresh** (`control: resync`). Nothing else ever calls them — not a timer, not a turn, not an MCP tool. Each launch/refresh stores a **snapshot** (`.cache/srs/<service>.json` with `fetched_at`) and the status registry persists to `.cache/srs/status.json`. **Every launch fetches the latest data** (user directive 2026-09-12); `SRS_CACHE_TTL_S` (default 0) is an opt-in guard that re-uses a snapshot younger than that many seconds instead, for rapid restarts during development. A snapshot in which some endpoints failed is stored with `partial: true` and is **not** re-used: the next launch fetches again regardless of age. An HTTP 429 ends the fetch for that service — no retry (ADR-024), every later request in that fetch refuses with the same reason — and the chip reads `stale` "rate limited" (an older snapshot, if any, is served). **The Bunpro MCP server makes no network requests at all**: its tools read the snapshot and every answer carries `synced_at` / `age_minutes`, so the tutor can say "as of your last sync". Consequently the MCP server needs **no token** — nothing secret enters that process, and `mcp.json` contains no credential. Measured 2026-09-09: launch fetch of both sources 4.6 s (budget 10 s), 6 Bunpro calls + 5 WaniKani calls; **8 Bunpro calls as of 2026-09-12** (the beginner, adept and seasoned levels joined the ghosts — §8b). `/user_stats/srs_level_overview` is verified and pinned in `constants.BUNPRO_READ_ENDPOINTS` but not fetched at launch.

**Bunpro credential:** the **Account API Token from Bunpro → Settings → API**, sent as `Authorization: Token token=<token>` **together with the query parameter `dangerously_authenticate_using_api_token=true`** and browser-like `Origin`/`Referer` headers of `https://bunpro.jp` — verified live 2026-09-09: without the parameter every endpoint returns 401 `AUTH_USER_DENIED`. (Bunpro's own naming flags this token as a full-account credential; the read-only transport is the mitigation.) `srs_level_details` takes a **named** `level` (`beginner|adept|seasoned|expert|master`); a numeric or missing level returns HTTP 500. That is the whole credential story — no email, no password, no browser-cookie scraping. Base origin `https://api.bunpro.jp` (what the current read-only community server hardcodes; confirm at M1, then it is a constant in `srs/http.py` — never a setting, §0 rule 7). Read endpoints: `/user`, `/user/due`, `/user/queue`, `/user_stats/jlpt_progress_mixed`, `/user_stats/srs_level_details?reviewable_type=Grammar`, `/user_stats/srs_ghost_level_details?reviewable_type=Grammar`, `/user_stats/forecast_daily`. Politeness: ≥ 1 s between requests (2 s × the session-start calls, eight since 2026-09-12, would not fit the 10 s budget — measured 2026-09-09), and **no retry at all** after a 429. Every response is validated against a pinned fixture so an upstream change fails loudly into the `bunpro_mcp` status chip instead of silently feeding garbage to the tutor. The server is launched by the claude subprocess via `mcp.json` and receives the token only through that entry's `env` block.

Render the collected data into a compact **Student Profile** (≤ 600 tokens) that is inserted into the tutor system prompt template below. Also write it to `logs/profile-<date>.json` for debugging.

### 5b. Service status indicators (SRS sync + MCP health)

The UI must show, at all times, whether each external dependency is actually working — not just configured. One compact status bar (chips, collapsible into the debug panel) driven by a `service_status` WS message per service, sent on every state change and at least every 30 s.

| service        | states                                                                                   | source of truth |
|----------------|------------------------------------------------------------------------------------------|-----------------|
| `wanikani`     | `disabled` (no token) · `syncing` · `ok` (level N, synced HH:MM — may carry `partial: N endpoint(s) failed`, in which case the snapshot is re-fetched next launch) · `stale` (serving cache; last fetch failed **or was rate limited**) · `error` (no cache, fetch failed) | the fetcher itself + cache timestamps |
| `bunpro`       | same set, for the session-start fetch (JLPT level, N ghosts)                             | the fetcher |
| `bunpro_mcp`   | `disabled` · `starting` · `connected` · `failed` · `used` (last tool call HH:MM, ok/error) | `init.mcp_servers[]` entry for the Bunpro server (reports `pending` at init — verified 2026-09-09 — so `connected` comes from the first successful `tool_result` or a later status event), then every `tool_use`/`tool_result` pair on the stream. The server itself only reads the snapshot; its "health" is "process up + snapshot present". |
| `claude`       | `starting` · `ready` · `thinking` · `rate_limited` (from `rate_limit_event`) · `fallback` (fallback model active) · `restarting` · `error` | the ClaudeSession event stream |
| `voicevox`     | `loading` · `warm` (engine version, N styles) · `ok` (engine up, styles not preloaded) · `down` | `GET /version`, then `POST /initialize_speaker` for every style in the emotion table — completed BEFORE Sensei's opening line, so `warm` means "can speak now". Re-checked on any synthesis failure |
| `stt`          | `loading` · `warm` (model name, VRAM MB) · `error`                                        | model load + warm-up |

Rules:
- Every state carries `detail` (short human string) and, for errors, `last_error` (sanitised — never a token, never a URL with a token in it).
- The client may send `control: resync` to force a WaniKani/Bunpro re-fetch, bypassing the cache TTL (still rate-limited). Show a spinner on the chip while `syncing`.
- `stale` is a first-class state, not an error: the tutor still has a profile, just an older one. Say so in the chip.
- `make doctor` prints the same table on the console, so "is Bunpro MCP alive" has one answer in both places.
- Status is also written into the session log header so a bad session can be diagnosed after the fact.

### 5c. SEARCH — the tutor finds its own subject to talk about (ADR-028)

A session that opens with 「今日はどうですか」 dies immediately. Sensei therefore opens on something concrete — news from Japan or the wider world, or whatever the student cares about — and turns it into conversation while deliberately working in their recent WaniKani vocabulary and current Bunpro grammar (§5, §6).

**She finds it herself.** The tutor is given a **search tool** backed by **SearxNG** — self-hosted metasearch, no API key, no third-party account, so the local-first property holds. It runs in `docker-compose.yml` beside VOICEVOX; one `docker compose up -d` starts both.

**News is one of four openers** (since 2026-09-12, §6c): lessons open in turn on news, an everyday scenario, something remembered about the student, or a short story built around today's targets. Only a `news` lesson searches at the opening; the other three cost nothing.

Boundaries:

- **A separate MCP server** — `backend/search_mcp.py`, exposing one read tool, `search` (`query`, `category`, `language` ∈ {`ja`, `en`, `all`} — an allowlist, default `ja`; anything else falls back to the default). It is **never** a fourth tool on the Bunpro server, whose exact three-tool surface the Golden Rule gate asserts (§0 rule 5).
- **Rationed by the prompt (§6):** search at the start of a session to find something worth discussing, and later only when the conversation genuinely needs a fact. Not every turn.
- **The cost lands where it is affordable.** The opening search happens before the student has spoken, where a second or two is invisible. The 5.0 s voice→voice budget (§10) governs conversational turns; any turn containing a tool call logs its tool time separately so the p90 measurement stays honest.
- **Results are untrusted text.** Titles and short snippets only, never full pages: control characters and `[`/`]` stripped (they would collide with the emotion tags of ADR-020), length-capped, count-capped. The tutor holds no built-in tools (§4), so a hostile result can at worst make her say something odd.
- **Optional, degrades cleanly.** No SearxNG reachable → the tool says so, the `search` chip reads `down`, and Sensei opens from the student's profile and the previous session instead. Startup never blocks on it. There is deliberately **no static topic list** — the fallback is her own curiosity, not a canned menu.

**Status: implemented and verified live** (2026-09-09, and multi-source 2026-09-10). This section previously said "not implemented"; that was stale — the MCP server has been answering in live sessions since M1.

**Sources are interleaved, not ranked by one engine** (2026-09-10, user directive). Measured on a Japanese news query, 47 of 57 SearxNG results came from `brave.news` alone, so taking the first few gave the tutor one source in practice. `news` results now draw round-robin across providers and are de-duplicated by title. Two SearxNG engines that were on by default are disabled in `docker/searxng/settings.yml` — `google news` (suspended behind a CAPTCHA) and `startpage news` (parse error): they returned nothing and each cost up to the request timeout on every search; disabling them took a search from several seconds to 0.7 s.

**Yahoo! JAPAN ニュース, through its official RSS** — not SearxNG, whose only Yahoo news engine is the English US site and is not even defined in the default news set. Nine topic feeds (top picks, domestic, world, business, entertainment, sports, IT, science, local) are listed in `backend/data/news_feeds.txt`, merged into `news` results only, and counted as **one provider** so nine feeds cannot fill every slot. Headlines older than 72 hours, or undated, are dropped. The tool is still one read tool, `search`; the feeds are sources behind it, not a second tool.

**NHK is deliberately excluded.** `https://www3.nhk.or.jp/rss/news/cat<N>.xml` still answers 200, but on 2026-09-10 every item across cat0/1/5/6 was 32–38 days old and `lastBuildDate` was 8–9 August: the feed is frozen (NHK moved to `news.web.nhk`). A 200 with month-old items is worse than a 404, because the tutor would present stale news as current — which is exactly what the age cutoff exists to prevent. NHK **News Web Easy** remains unusable: `news-list.json` returns `401 missing_token`.

## 6. TUTOR SYSTEM PROMPT (template — render with soul, profile and topic; versioned files under `prompts/`)

The prompt is assembled from versioned, user-editable files under `prompts/` and runtime values, in this order — persona first, hard rules last, so the rules win by position. Every model-facing text lives in a file (ADR-012): the template `tutor.md`, one persona file per tutor, and the memory headings `memory.md`, the rotation handoff heading `handoff.md`, and the summariser's instructions `summarise.md` with its system prompt `summariser.md` (§6b). Known deviation: `backend/explain.py` still holds the explainer's short system text in Python, to move later.

| Placeholder | Source | Cap |
|-------------|--------|-----|
| `{{soul}}` | **`prompts/<persona>.md`** (`tanaka`, `hayashi`, `minami`, `mori`; `TUTOR_PERSONA` selects — ADR-026 as amended by ADR-030) — who the tutor is: background, life, manner, and the `<!-- voice: NN -->` declaration. Persona may colour *how* she speaks, never override the HARD OUTPUT RULES below. | ≤ 400 tokens |
| `{{student_profile}}` | Rendered from WaniKani + Bunpro at launch (§5) | ≤ 600 tokens |
| *(no topic placeholder)* | Sensei finds the subject herself with the `search` tool (§5c, ADR-028) | — |

`prompts/tutor.md` holds the template below and is likewise never hardcoded in Python (ADR-012).

```
You are 先生 (Sensei), a warm, sharp Japanese conversation tutor having a REAL-TIME VOICE conversation. Your text is synthesized to speech — write ONLY what should be spoken aloud.

STUDENT PROFILE
{{student_profile}}   <!-- WaniKani level, recent vocab, leeches, Bunpro grammar state -->

HARD OUTPUT RULES (voice pipeline constraints)
- Speak in Japanese by default. Short sentences: ≤ 25 characters each, 1–3 sentences per turn unless explaining grammar.
- NO markdown, NO lists, NO romaji, NO furigana notation, NO parentheses asides, NO emoji. Plain spoken Japanese only.
- Numbers and dates in kanji/kana as they would be SPOKEN (二千二十六年, not 2026年 read ambiguity — write にせんにじゅうろくねん if reading could be wrong).
- Rare/above-level kanji words: write them in kana so TTS reads them correctly.
- Begin the turn, and optionally any later sentence, with exactly one emotion tag from: [happy] [thinking] [surprised] [serious] [encouraging] [proud] [confused]. The tag drives your face and your voice tone, so choose it for how that sentence should SOUND: [happy] for praise and warmth, [thinking] when working something out or asking them to try again, [surprised] for a genuinely good answer or an unexpected turn, [serious] for a correction that matters, [encouraging] when pushing them to attempt something, [proud] for real praise after real effort, [confused] when you genuinely did not understand. No tag means neutral. Nothing else in brackets, ever.
   (The seven are `chunker.EMOTIONS`; the live wording, with the study tags of §8b, is in `prompts/tutor.md`.)

TEACHING BEHAVIOR
- Match the student's level: prefer vocabulary from their recent WaniKani unlocks and grammar at/below their Bunpro level. Deliberately reuse their leeches and ghost-review grammar in natural contexts — that is your superpower.
- Correction policy: minor errors → recast naturally (repeat their idea correctly) and move on. Meaning-breaking errors → briefly stop, give the fix in one sentence, have them retry. Never lecture mid-conversation for more than two sentences; offer 「詳しく説明しましょうか」 instead.
- If the student says 「英語で」/"in English", switch to concise English for the explanation, then return to Japanese.
- If the transcript seems garbled (STT error), don't guess wildly — ask 「もう一度言ってもらえますか」naturally.
- You may use the Bunpro tools to check their current review queue when they ask what to practice, or roughly every 15 minutes — not every turn.
- End of session (user says goodbye): give a 3-sentence summary in Japanese of what they did well and one thing to review.
- Politeness register: default です・ます. If the student consistently uses plain form, mirror it.
- Open the session by finding something worth talking about: use the search tool once for recent news (Japan or the world) or something the student has shown interest in, pick ONE thing, say something of your own about it, and ask them a question. Never read results aloud or summarise the news — it is a way in, not a lesson. If search is unavailable, open from what you know about them or from last session.
- Use search again only when the conversation genuinely needs a fact. Never every turn.
```

**DON'T** hardcode this prompt in Python. **DO** load from file so the user iterates on it without touching code.

## 6b. MEMORY AND CONTEXT (ADR-031, ADR-032)

**The rule everything here follows: nothing that is not speech goes on the critical path.** The
path from the student stopping speaking to the first audio coming back is VAD → STT → brain → TTS
and nothing else. Memory reads, memory writes, summarising and session rotation all happen either
at session start, at session end, or in the **speaking gap** — the seconds after a turn completes
while the avatar is still playing synthesised audio and the orchestrator is idle. All of it is
best-effort and cancellable: if the student speaks, the conversation wins and the background work
is abandoned.

### Five tiers of memory

| tier | where | read | written | budget |
|---|---|---|---|---|
| turn log | `logs/sessions/<date>-<session>.jsonl` — one file per lesson; the date is fixed at launch, so a lesson crossing midnight stays one file | never by the tutor | appended in the speaking gap | — |
| student notes | `<state>/memory/student.md` | session start → prompt | summarised at next launch | `MEMORY_MAX_TOKENS` |
| about the student | `<state>/memory/about-me.md` | session start → prompt | summarised at next launch | shares the above |
| last-session brief | `<state>/memory/<tutor>/last-session.md` | session start → prompt | summarised at next launch | shares the above |
| recent topics | `<state>/memory/<tutor>/topics.jsonl` | session start → prompt | summarised at next launch | shares the above |
| about this tutor | `<state>/memory/<tutor>/facts.md` | session start → prompt | summarised at next launch | shares the above |

- **They know each other** (user, 2026-09-12). Two short lists carry the relationship: what is
  durably true of the student — their name, the country they live in, their work, their cat — and
  what this tutor has said about **their own** life, so they do not acquire a second pet next week.
  Ten lines and six, one fact each, hand-editable; over the cap the oldest survive (a name is
  learned in the first lesson and must not be pushed out by last Tuesday's cake) with the newest
  few always given a slot. A tutor is a man or a woman depending on the chosen voice, so their
  facts are written without pronouns.
- **Memory is per tutor** (user, 2026-09-12). What is true of the student is shared by everyone who
  teaches them; a lesson, though, happened between two particular people, and a tutor's own life is
  their own. So `<state>/memory/<tutor>/` holds the brief, the topics and that tutor's facts, and
  switching `TUTOR_PERSONA` — at launch or live from the settings panel — switches the whole
  drawer. The turn log records who taught each lesson, and only that tutor summarises it. A memory
  written before the split belongs to whoever is teaching when it is first read.

- **Read once, at session start.** Both files are rendered into the system prompt beside the soul
  (§6) and the SRS profile (§5), through the same budgeting that truncates at a line boundary and
  reports what it cut. After that, the model has everything it will get.
- **There is no memory tool and there will not be one.** If it was not loaded at start, the tutor
  does not know it. A tutor who says 「あれ、なんだっけ」 beats one that stalls.
- **Write in the gap.** The turn record is appended when `TurnComplete` fires, never while a turn
  is in flight.
- **The launch is one wait, not several** (amended 2026-09-12, user). The summary of the last
  lesson starts at the top of `run()` and runs while the SRS snapshot, the page, VOICEVOX and
  Whisper all come up; it is awaited at the bottom, immediately before her system prompt is
  assembled and her session spawned. She therefore never speaks having forgotten yesterday, and
  the student never waits twice for the same seconds. A session nobody spoke in is marked
  summarised without a model call, and an answer of "there is nothing here" is not asked again —
  three such logs were re-read at every launch (live, 2026-09-12).
- **Summarise at the next launch** (amended 2026-09-10), as a separate short-lived `Brain` on a
  cheap model (`MEMORY_SUMMARY_MODEL`, default `haiku`) whose input is a text-only excerpt of the
  turn log — deterministic and re-runnable. Originally this ran at session end with the next launch
  as the fallback; the fallback is now the path. Exit has to be instant — a Ctrl+C that hangs for
  fifteen seconds reads as a crash — whereas launch is init time the student already waits
  through. A session is "summarised" when `topics.jsonl` holds a row for it — keyed by
  `(date, session)`, the two things a log's name carries — so there is no second bookkeeping
  file to drift. Only a failed **call** stays pending — a provider error as much as a timeout; an
  answer that says there is nothing here, or that is not JSON, is marked done and never asked
  again. The summariser's instructions are `prompts/summarise.md` and its system prompt
  `prompts/summariser.md` (ADR-012).
- **Recent topics stop the lessons repeating themselves** (added 2026-09-10). Each summarised
  session contributes at most five short noun phrases; the last eight sessions' worth are rendered
  into the prompt as *recently discussed — do not open on these*, de-duplicated and newest first.
  It is the cheapest possible anti-repetition: one line of prompt, no retrieval, and it directly
  targets the failure a student notices first — three lessons in a row opening on the same news.
- **The opening offers a choice** (added 2026-09-10, user request). When a last-session brief
  exists, the tutor greets, recalls it in one sentence, and asks: carry on with that, or something
  new? It waits for the answer before searching. Carrying on picks the thread back up with *today's*
  SRS profile — refreshed at launch, so the goals are current even when the topic is not. Something
  new, or a first-ever session, opens on one search (§5c) that avoids recent topics. The rule lives
  in `prompts/tutor.md`; the opening nudge in code stays neutral. Bonus: a continuing session skips
  the opening search, and its latency, entirely.
- **`student.md` is markdown the user edits.** A wrong memory recalled confidently is worse than no
  memory, and the correction mechanism is a text editor. It holds grammar points missed more than
  once, vocabulary the student produced *unprompted*, topics that got them talking, and facts about
  their life. **Not** transcripts, and **not** anything already in the SRS profile — WaniKani and
  Bunpro are the authority on what is being studied (ADR-024) and duplicating it lets the two
  disagree.
- **Memory files live outside the repo**, in the per-user state directory, gitignored, never
  committed, never sent to the browser. They hold the student's life (§11).

### Turn log schema

One JSON object per line, appended, never rewritten. This is also the user's future Anki mine, so
the schema is a stable contract — add fields, never repurpose them.

```
{"ts", "session", "turn",
 "student": {"text", "audio_ms", "stt_ms"},
 "tutor":   {"text", "sentences": [{"text", "emotion", "synth_ms"}]},
 "tools":   [{"name", "ok", "ms"}],
 "latency": {"ttft_ms", "first_audio_ms", "first_play_ms", "voice_to_voice_ms"},
 "usage":   {...as the provider reported it...}}
```

`tools` and `usage` are filled on the voice path too (since 2026-09-12), from the brain's
`last_turn`; they were empty there before. `latency.first_play_ms` was added the same day
(additive — §10 says what it measures); the WS `timing` message carries it as well.

Corrections are **not** marked live: the tutor emits emotion tags and nothing else, so the voice
path stays exactly as §7 and ADR-020 specify. Mistakes are mined from the log by the end-of-session
summariser, off the critical path.

### Context rotation

A long lesson fills the model's window, and the provider then compacts on its own schedule. In a
voice conversation that is the tutor going silent for several seconds with no explanation — the
worst latency event this design can produce, arriving exactly when the lesson has been going well
long enough to fill a window.

- **Measure it, don't wait for it.** Every `TurnComplete` carries `usage`; for this CLI,
  `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` is what the model read
  that turn and tracks live context size for free.
- Above `CONTEXT_ROTATE_AT` — a fraction of the window **the provider reports every turn**, never a
  hard-coded token count — arm a rotation. Where the provider compacts on its own is its policy:
  nothing reports it and it can move without a release of ours (verified 2026-09-11). So it is
  never assumed nor measured once — it is **watched for**. The provider announces each compaction
  on the stream; the UI explains the silence, the turn records it, and an automatic compaction
  that beats the threshold lowers it for every later session.
- **Rotate in the speaking gap:** build a handoff brief from the turn log (deterministic, no model
  call), spawn a second process under the §4 rules with a fresh `--session-id`, prompt = the usual
  sections plus the brief. The brief is the **newest** lines of *this launch's* turns that fit
  `HANDOFF_MAX_TOKENS` (600), under the heading in `prompts/handoff.md`; before the launch's first
  turn it is empty and omitted entirely, so a replacement spawned then — a Refresh, a tutor switch
  — greets normally: the first lesson of the day is greeted (user directive 2026-09-12).
- **Swap at a turn boundary, never inside one.** Not ready when the student speaks? Keep the old
  process and try the next gap. Rotation is never the reason a turn is slow.
- If rotation keeps failing, let the provider compact and **log it as a latency event** so §10
  instrumentation shows it for what it is.
- `--resume` (§4) resumes whichever session is authoritative — the old one, until the swap lands.

**Tool output is summarised before it enters context.** A search result set or an SRS snapshot
pasted whole is thousands of tokens re-read on every subsequent turn for the rest of the session.
Capping it is cheaper than rotating more often.

## 6c. STUDY PLAN — ROTATING TARGETS (ADR-038, user request 2026-09-12)

The problem: lessons revolved around a few news items and the top of the grammar list. The student
wants diversity and coverage of **all** the vocabulary and grammar they have not mastered, with the
target set rotating — an item they have replied well with a few times progresses out and the next
candidate takes its place. The constraint: no additional model calls.

**What it is.** `backend/study_plan.py`, deterministic, milliseconds, built at launch from two
things that already exist: the SRS snapshot (§5 — never a new call, ADR-024) says what they are
still learning, and the turn logs (§6b) say what has been practised. Nothing is persisted: the fold
is recomputed at every launch and is the source of truth.

- **Coverage ledger.** Every session log on disk — all tutors, the student is the same — the last
  60 sessions, replayed in the order they happened. Per item (keyed by `study.normalise`):
  `heard` (the tutor used or asked for it: her `{{span|point}}` marks, `[target:]`, or one of
  their words matched in her sentence), `produced` (her `[used:]` credit), `attempted` (matched
  in the student's transcript — their words by `backend.study`, a grammar point by its normalised
  name), `last_seen`, `last_produced`, and the SRS part: `streak`, `lapses`, `due_session`,
  `progressed`.
- **It behaves like an SRS** (user refinement, 2026-09-12). When a replayed session closes, for
  each item the tutor used or asked for: produced ≥ `STUDY_PROGRESS_AFTER` times → a success
  round, `streak += 1` and due again at `session + min(STUDY_SPACING_MAX, STUDY_SPACING_BASE ×
  2^(streak−1))` — 1, 2, 4, 8, 16, 32 sessions; attempted but never produced correctly → a lapse,
  streak 0, `lapses += 1`, due next session; used by the tutor but never attempted → unchanged,
  still due. A never-seen item is due at 0. The better it is handled, the rarer it returns; a
  painful one comes back soon.
- **Selection.** `STUDY_TARGET_VOCAB` (8) words from everything below Guru plus the leeches;
  `STUDY_TARGET_GRAMMAR` (4) points from the ghosts and everything still in their Bunpro SRS.
  Candidates are the items due (`due_session ≤ today`), ranked: a target that had a success round
  within the last 7 days goes to the back — without this the weakest few come straight back at a
  one-session gap and the rest of the list never gets a turn — then weakness (ghost > leech >
  the lower SRS stage), then never-covered before covered, then the most overdue, then the most
  lapses, then the fewest productions, then the text, so two launches over the same logs choose
  the same targets. Too few due → filled with the soonest-due. Everything left is a ranked
  **queue** for mid-session replacement.
- **Progression, live.** After every turn the plan reads the same record memory does. A target
  produced `STUDY_PROGRESS_AFTER` times this session is retired — its next due is set at once, so
  a Refresh later in the session cannot bring it back — and the next queue candidate of the same
  kind is promoted. The terminal says so (`[study] X progressed (back after N sessions) -> new
  target Y`).
- **Four openers, one per lesson in turn:** `news` (the §5c search — the only opener that may
  search), `scenario` (an everyday situation from `backend/data/scenarios.txt`, picked by striding
  through the list with the scenario ordinal and skipping anything that overlaps a recent topic),
  `personal` (a remembered fact about the student, §6b; with none, a scenario), `story` (three
  sentences of her own around three of today's targets, then a question). The index is the number
  of lessons this tutor has summarised. With a LAST SESSION the greeting and the carry-on question
  still come first (§6b); the opener applies when they choose something new.
- **In the prompt.** A `{{study_plan}}` slot right after the profile (§6): TODAY'S TARGETS — the
  words with reading and meaning, the grammar points as Bunpro names them, the OPENER line, and the
  rule: at least one target in every turn, elicit each target at least once, a target produced
  correctly twice is done — move on. ≤ 250 tokens (`STUDY_PLAN_MAX_TOKENS`, tested), and carried
  into every session of the launch — rotated, refreshed, re-personed — through the same path as
  the profile. The template's opening rule now follows the OPENER line, and TEACHING BEHAVIOR says
  the targets are the priority list.
- **Coach notes.** Every `STUDY_NUDGE_EVERY` turns (3; 0 = off), and always on the turn after a
  progression, a bracketed English note of ≤ 40 tokens is put above the student's words in the
  text the brain is asked: `[coach: not yet used: 貯金、〜たら; elicit 〜ておく next; 〜てみる
  progressed (back after 2 sessions) → new target 〜ながら]`. Only the brain sees it: the turn
  log records the raw transcript, the page's `stt_final` and the TTS never carry it
  (`VoiceLoop.coach`, `orchestrator.one_turn(coach=)`). The tutor is told the notes are the
  system's, never the student's, never to be read aloud or answered.
- **Summariser.** Two lines under the excerpt — targets practised and progressed, from the log's
  own marks, no item set needed — and an optional `progressed` key in the summary JSON, kept on
  the topics row. A summary without it lands as before.
- **Words in files** (ADR-012): `prompts/coach.md` holds the block's wording, the opener lines,
  the note template and the summariser's two headings; the rules are in `prompts/tutor.md`.

Config (§11): `STUDY_TARGET_VOCAB` 8, `STUDY_TARGET_GRAMMAR` 4, `STUDY_PROGRESS_AFTER` 2,
`STUDY_NUDGE_EVERY` 3, `STUDY_SPACING_BASE` 1, `STUDY_SPACING_MAX` 32. Cost: zero model calls;
about 250 cached prompt tokens per turn and a 40-token note every third turn. Known crudeness:
grammar "attempted" is a substring match of the point's name in the transcript, so a false lapse
is possible — it only brings an item back sooner, never later. Progress depends on the tutor's
`[used:]` credit: if she under-credits, nothing progresses, which the terminal line makes visible.

## 7. TTS + LIP-SYNC (VOICEVOX → Oculus visemes)

- `POST /audio_query?text=<sentence>&speaker=<id>` → JSON with `accent_phrases[].moras[]` (each mora: `consonant`, `consonant_length`, `vowel`, `vowel_length`) plus `pause_mora`, `prePhonemeLength`, `postPhonemeLength`, `speedScale`.
- `POST /synthesis?speaker=<id>` with that JSON → WAV bytes.
- Build the viseme timeline by walking morae and accumulating time. Honor `prePhonemeLength` offset and `speedScale` (divide durations by it). `pauseLengthScale` is applied to every `pause_mora` before the speed division (`VOICEVOX_PAUSE_SCALE`): the semantics are read from the engine source and pinned as **UNVERIFIED** in `constants.py` (`VOICEVOX_PAUSE_SCALE_VERIFIED = False`) until a live synthesis check compares the WAV at two scales — ROADMAP live checks. Timing unit: output milliseconds relative to audio start (TalkingHead's `vtimes`/`vdurations` convention — confirm ms vs s against its README and pin).
- Mora → Oculus viseme mapping (implement as one pure, unit-tested function):
  - vowels: a→`aa`, i→`I`, u→`U`, e→`E`, o→`O`
  - N (ん)→`nn`, cl (っ)→`sil`, pau/pause_mora→`sil`
  - consonant prefix (shorter, before vowel): k,g→`kk`; s,z,sh,j,ts→`SS`; t,d→`DD`; ch→`CH`; n→`nn`; m,b,p→`PP`; f,h→`FF`; r→`RR`; w,y→skip (let vowel dominate)
  - Devoiced vowels (VOICEVOX marks uppercase vowel e.g. `U`): use the shape but the frontend caps its weight at ~0.4.
- **The speaker id comes from the persona, not from config** (ADR-030). `prompts/<name>.md`
  declares `<!-- voice: NN -->`; the declaration is stripped before the file reaches the model.
  `VOICEVOX_SPEAKER=-1` (default) means "ask the persona"; a real id overrides, for auditioning.
  `speedScale` (default 0.9 for learners) and `intonationScale` stay in config.
- **A voice is shortlisted by measurement and chosen by ear** (ADR-030). Before any voice may be
  declared in a persona file, sweep the whole `GET /speakers` catalogue on one fixed sentence and
  record: **F0 jitter** (mean frame-to-frame |dF0| / mean F0, autocorrelation at a 5 ms hop,
  reported in cents so registers compare), **spectral flux** (mean L2 change of the normalised
  magnitude spectrum — the "rough between phonemes" axis), **shimmer**, and **synthesis ms per
  sentence** (charged against §10). The numbers produce a shortlist; a human picks from it.
  Do **not** auto-select the metric winner: `tanaka` deliberately runs on the least steady voice
  in the catalogue because on a fifty-year-old that unsteadiness reads as age. Method and the full
  sweep are in ROADMAP V0.3.
- **Synthesis cost is a persona property**, ~615-1010 ms/sentence across the voices auditioned.
  Measure the §10 voice→voice p90 against the *configured* persona, never against one number.
- **Voice tone follows the emotion tag.** VOICEVOX speakers expose multiple *styles* (each style is its own `speaker` id — e.g. a character's ノーマル / あまあま / ツンツン variants) and `audio_query` accepts `pitchScale` / `intonationScale` / `speedScale` overrides. Emotion → voice is a config table, one row per emotion, per chosen speaker:
  ```
  neutral:   style=<base style id>,  speed=0.90, pitch=0.00, intonation=1.00
  happy:     style=<cheerful style>, speed=0.95, pitch=+0.02, intonation=1.15
  thinking:  style=<base>,           speed=0.85, pitch=-0.01, intonation=0.90
  surprised: style=<base>,           speed=1.00, pitch=+0.04, intonation=1.30
  serious:   style=<calm style>,     speed=0.85, pitch=-0.03, intonation=0.85
  ```
  The table has one row per tag in `chunker.EMOTIONS` (the seven of §6; the five above are the original starting points, `backend/emotions.py` holds all seven). Enumerate the installed speaker's real style ids from `GET /speakers` at startup (**V0.3** pins the endpoint shape); if a mapped style id does not exist, log a warning and use the base style with the scalar overrides only. An engine that is unreachable at startup (a container still booting) leaves the table unresolved; `ensure_table()` rebuilds it lazily on the first successful call, so a late-booting VOICEVOX switches to the real styles from the first sentence that reaches it.
  For a speaker with **only one style** (麒ヶ島宗麟, 栗田まろん, 冥鳴ひまり …) every emotion lands on the same voice, so the scalars are widened to keep the emotions apart — but **pitch only** (`SINGLE_STYLE_PITCH_SPREAD` 1.8), never intonation (`SINGLE_STYLE_INTONATION_SPREAD` 1.0) — and only when the catalogue **confirms** the speaker has one style: an empty catalogue is a question not yet answered, not a single-style speaker. Widening `intonationScale` stretches the model's own F0 wobble along with the contour: measured 2.15% -> 2.41% jitter at 1.8x, audible as an unsteady voice between phonemes. `pitchScale` is a constant offset in log-F0 and carries the emotion at no stability cost. Numbers above are starting points — tune by ear in M3. The emotion applies to the sentence carrying the tag and every following sentence until the next tag or the end of the turn.
- **DO** synthesize sentence-by-sentence as chunks arrive from Claude; **DON'T** wait for the full reply.
- **DON'T** send text to TalkingHead's `speakText` — its text lip-sync has no Japanese module. ALWAYS use `speakAudio` with the audio + viseme timeline.

## 8. FRONTEND

- TalkingHead init with the GLB avatar, lipsyncModules can be empty (we always pass visemes explicitly).
- WebSocket client with auto-reconnect. Message protocol (define as typed constants shared in one place, mirrored in Python pydantic models; a contract test asserts the two sets are identical):
  - client→server: `control` (`start`, `stop`, `cancel`, `resync`, `quit`, `new_topic`, `ready` — `start`/`stop` are the push-to-talk edges, and `cancel` is **ALT GR during a hold** (the right-hand ALT: Chrome claims SPACE with the left one): what has been recorded is dropped, the talk key can then be released without sending anything, and the next press starts clean (user, 2026-09-12); `quit` shuts the orchestrator down cleanly from the page, and is deliberately not `stop`; `new_topic` is the page's New topic button — she drops the subject, interrupting herself if need be, and searches for a fresh one exactly as if the student had said 「話題を変えて」), `settings` (partial update of **any** key in the `config.py` schema, secrets included — §11; the server validates, persists to `settings.json`, applies live where possible, and replies with the applied `settings` echo in which secrets appear only as `{set, hint}`. `model` takes effect by respawning the claude subprocess with `--resume`, reported via `service_status: claude=restarting`; token changes re-run the session-start SRS fetch), `explain` (`{kind, text, context, lang}` — §8b). There is **no** audio message from the page: the orchestrator captures the microphone (§2).
  - server→client: `state` (`listening|thinking|speaking`, with the turn epoch), `stt_partial` (**reserved**: STT runs on complete utterances, so partials are not produced in M2–M5; the type exists so a streaming-STT experiment does not need a protocol change), `stt_final`, `speak` (`{audio_b64, visemes[], vtimes[], vdurations[], text, emotion, turn, grammar[], target, used, used_kind, readings[], vocab[]}` — the sentence's text is the subtitle and the chat bubble, and its `emotion` rides with its audio), `bargein`, `service_status` (§5b), `settings` (echo), `mic_level`, `meters` (context use, §6b), `timing` (per-turn stage breakdown, §10), `explanation` (§8b), `history` (the lesson so far for a page that connects, §8b; `state` carries `spoken`), `error`. `grammar[]` entries carry `level`, `vocab[]` entries `leech`. This is the whole set, generated from `backend/models.py` (gate M3a); there is no separate `assistant_text`, `emotion` or `srs_profile` message and no client `bargein_ack` / `settings_test` / `audio_chunk`.
- **The handshake checks `Origin`** (ADR-017, 2026-09-12): a browser always sends one, and only the page's own origin is accepted — `http://127.0.0.1`, `localhost` or `[::1]` at `PORT`, plus the Vite dev server on `5173` (whose proxy forwards the browser's Origin unchanged). Anything else — another site's, or the `null` of a sandboxed frame — is refused **before** the socket is accepted. A client with no Origin at all is not a browser (a test client, a script) and is admitted only from the loopback address itself.
- **Each page has its own outbox** and sends never block the conversation: `mic_level` is coalesced (only the newest is kept), everything else — a sentence, a state, a barge-in, a settings echo — is delivered in order however slow the page; a page whose outbox reaches `OUTBOX_MAX` has stopped reading and is closed so it reconnects. A page that drops off the socket **mid-hold** has its hold cancelled as its own ALT GR would, so the microphone is never wedged open by a vanished tab.
- **The avatar is optional at runtime.** If TalkingHead or the GLB cannot load (CDN down, page offline, file missing), the page says so on the stage and falls back to **audio-only playback** (`frontend/src/audio_only.ts`, the same queue contract as the head): every sentence still plays and still lands in the chat as its audio starts.
- UI: avatar full-viewport, waist-up camera framing; subtitle strip (toggle: JP / off — there is no English text source, since the tutor speaks only Japanese and 「英語で」 already gets an English explanation *spoken*; an EN subtitle mode would need a translation path and its latency cost, so it is explicitly out of scope until someone asks for it); mic state indicator; session timer; **service status bar** (§5b) with the settings button at its right end — a floating cog over the scene was in the way of both the avatar and the study panel (user, 2026-09-12); settings drawer (voice speed, VAD sensitivity, model, subtitles); a one-line "headphones recommended" hint until the first successful barge-in.
- Idle life: auto-blink (random 2–6 s), subtle procedural sway, `lookAt` camera. **Eye contact is held at ~0.9 idle and speaking** (`avatarIdleEyeContact` / `avatarSpeakingEyeContact`, both [0,1]): TalkingHead defaults to 0.2/0.5, which makes the tutor look away most of the time and reads as evasive rather than attentive. Not 1.0 — unbroken eye contact is a stare (verified 2026-09-10). **Listening reactions:** while `state=listening` and the server reports speech (VAD `speech_start`), the avatar shifts to an attentive pose and gives a small nod on each detected pause ≥ 300 ms — the あいづち a human tutor would give. While `thinking`, a subtle "considering" idle (gaze up-and-away, slight head tilt).
- **Emotion → rig.** One table, in `frontend/src/avatar.ts`, pinned against the TalkingHead README's actual mood and gesture names (**V0.4**):

  | tag         | TalkingHead mood | extra                                             |
  |-------------|------------------|---------------------------------------------------|
  | (none)      | `neutral`        | —                                                 |
  | `happy`     | `happy`          | —                                                 |
  | `thinking`  | `neutral`        | gaze up-and-away, slight head tilt, brows slightly down |
  | `surprised` | `neutral`        | brow-raise + eye-widen morph override, brief; drop back after ~1 s |
  | `serious`   | `neutral`        | brows slightly down, no sway, hold gaze           |

  Moods crossfade over 300 ms. Where TalkingHead has no matching mood (surprised, serious), drive ARKit blendshapes directly via its morph-override facility — the exact API is pinned in V0.4, not assumed. The table covers all seven tags of §6 (`frontend/src/rig.ts`; `test_models.py` asserts every emotion the voice knows the face knows). Emotion is set **when the sentence carrying the tag starts playing**, not when the tag is parsed — otherwise the face reacts a full TTS latency before the voice does. The `speak` message therefore carries the sentence's `emotion` alongside its audio. **The expression settles at the end of the turn, not on a timer** (ADR-020, amended 2026-09-12): each sentence's release timer is cancelled by the next tag, and only the end of the turn schedules the settle back to neutral — a fixed 4 s timer put her back to neutral mid-sentence. Tags are **case-insensitive**; a TalkingHead mood name in brackets (`[angry]`, `[sad]`…) is still a tag — stripped so it is never spoken, recorded as stray, ignored for the emotion — and `[neutral]` alone resets to neutral.
- Barge-in is explicit: pressing the talk key while `speaking` stops playback on the key event itself, before the server hears of it, and the server confirms with `bargein` (closing the turn epoch). In `TURN_MODE=vad` the server VAD decides and the page stops on `bargein`.
- **Self-barge-in (the avatar interrupting itself).** The mic hears the speakers. With push-to-talk (the default, §9) the tutor's own voice cannot end a turn, which is the strongest layer. For `vad` mode, three layers, all required: (1) capture-side echo cancellation — the orchestrator owns the microphone (§2), so the browser's `getUserMedia` constraints do not apply and this layer is the OS/device's; (2) while `speaking`, the server VAD uses a **higher threshold** (config `BARGEIN_THRESHOLD_FACTOR`, default 2.0 — note the server VAD emits a *probability* capped at 1.0, so the factor divides the headroom to certainty, `1-(1-t)/f`, giving 0.75 at t=0.5: multiplying would put the bar at 1.0 and silently disable barge-in, verified 2026-09-09) and require ≥ 250 ms of sustained speech before firing; (3) the server ignores VAD `speech_start` events during the first 150 ms of playback (loudspeaker onset). M3 acceptance includes a **10-turn conversation on laptop speakers with zero self-interruptions**, in addition to the headphones run.
- The page does not capture audio. The orchestrator captures the microphone (sounddevice, 16 kHz mono, `backend/audio.py`) and the page only controls the turn: push-to-talk `control: start`/`stop`/`cancel`, and **losing window focus (`blur`) cancels a capture in progress** (§9b) — a key release the page never sees must not leave the hold open. The original design (ADR-006) streamed browser PCM over the socket; the socket now carries no microphone audio.
- **DON'T** add build complexity: no React, no state library. One page, a few modules.

## 8b. STUDY PANEL (ADR-036 — user request 2026-09-11. Built 2026-09-12: the layout, the chat, the tutor's marks coloured by SRS level, the hint, per-kanji furigana, the word you used floating behind her, explanations and translations on click, the transcript replayed on reload. Not yet: word cards beyond their own WaniKani words)

- **The wait has a face** (user, 2026-09-12). Until her first sentence the chat holds one bubble
  with three breathing dots and a caption taken from whichever service is still coming up —
  "reading back your last lesson…", "loading speech recognition…", "warming her voice…", "she is
  thinking of how to start…". It is removed by her first sentence. A launch that goes quiet for
  half a minute reads as a hang, and the chips alone were not enough.
- **New topic asks first** (user, 2026-09-12): the round button opens a confirm card rather than
  dropping the subject, because it interrupts her and throws away what you were in the middle of.
- **Layout.** The avatar takes the left of the page; the conversation runs on the right as a
  message thread — her bubbles on one side, the student's on the other, newest at the bottom,
  following the conversation as it grows. `STUDY_PANEL=off` returns to the full-width avatar with
  subtitles.
- **Everything still in their SRS is in play** (user, 2026-09-12): "she must use all the forms I
  haven't Guru'd or mastered, with a preference for the all new fresh stuff". Bunpro is read for
  ghosts *and* the beginner, adept and seasoned levels (three more GETs at launch and Refresh only,
  ADR-024), which took this student from 2 ghosts + 10 beginner points to **68 points in play**.
  The profile lists them weakest first — the first ten with their meanings, the rest by title,
  which is all she needs to use a form — and the prompt tells her to work THROUGH the list rather
  than orbit the top of it. The chat matches all of them, not only the ones that fit the prompt.
- **Blue is one of their own words** (user, 2026-09-12): **every** vocabulary item the student has
  not yet Guru'd (WaniKani stage < 5 — not only the newest thirty, which left her a palette of 21
  words and 13 % of her sentences carrying one) and the grammar they have not mastered, from the
  snapshot already on disk —
  `backend/study.py`, no model call and no fetch (ADR-024). **The tutor marks her own uses of
  their words**, exactly as she marks grammar — `{{申します|申す}}`, named as WaniKani writes the
  item (user, 2026-09-12: she is the one who knows which of her words is one of theirs) — and a
  mark whose name is on the vocabulary list becomes a word span, one on the grammar list a
  grammar span. Where she forgot, `Study.spans` finds a word by **whole tokens only**: a token's
  surface, base form or lemma, or the join of adjacent tokens, must equal the item
  (聞こえにくかった → 聞こえる, たけ → 竹 through the lemma). Never a piece of a token and never a
  reading: 申す reads もうす, and the もう of もう一度 was painted with it until the reading and
  stem forms were removed (user, 2026-09-12, screenshot). Without the tokenizer only the written
  form counts. Where a word sits inside a grammar point the grammar wins, because two nested
  marks are a box inside a box. The same lists decide the **float behind her**: `[used:…]` is
  looked up, `speak.used_kind` says whether it was one of their words or one of their grammar
  points, and it drifts up in that colour. The student's own turn floats one too, the moment the
  transcript arrives: that one is objective (the word is theirs and they said it), while her credit
  is the only one that can be a grammar point — gold when the tutor credits something from
  neither list, which is also how a tutor crediting the wrong thing shows up.
- **Every mark wears the colour of its SRS level** (user, 2026-09-12, replacing "red is grammar,
  blue is a word"): the Bunpro scale — ghost grey, beginner dark teal, adept navy, seasoned purple,
  expert pink, master rose — for a grammar point's own Bunpro level, and for a word its WaniKani
  stage mapped onto the same scale (Apprentice → beginner, Guru → adept, Master → seasoned,
  Enlightened → expert, Burned → master; a leech → ghost). Grammar is a solid bar, a word a dotted
  one, so the kind stays readable; a legend of the seven swatches sits in the chat header. A
  point the tutor names that is on neither list keeps the neutral grammar colour and still goes
  through the guard below. Grammar is a conjugation, an auxiliary or a pattern, wrapped whole —
  〜てみよう is marked from the stem, not from its tail; a noun, a plain verb or adjective, a name
  or a number is a word, marked only when it is one of theirs. `Annotator.grammar_only` keeps an
  unlisted all-noun span from going red, with the tokenizer already loaded for furigana.
- **A point she forgot to mark still goes red** (user, 2026-09-12: 「と思います」 stayed black
  although 「と思う」 was on their list). `Annotator.find_points` looks for every grammar point on
  the student's list by the tokenizer's **base forms** — 思います and 思っ both read 思う — through
  the auxiliaries that finish the form, with a gap in the point's name (あまり～ない) allowed to
  hold anything. Conservative: a point that is one short kana token (ば, なら, かな) is never
  guessed, and her own marks win where they overlap.
- **A blue word is clickable too** (user, 2026-09-12), and its card needs no dictionary: the kana
  reading, the English meaning and the WaniKani stage travel with the sentence in `speak.vocab`,
  because they are the student's own items and we already hold them. **The card shows the level
  of mastery** (user, 2026-09-12): the WaniKani stage name ("Apprentice 4") or the Bunpro level
  ("Ghost", "Beginner"…) as a chip in that level's colour. On'yomi and kun'yomi per kanji still
  wait for KANJIDIC2 (ADR-036 point 3).
- **Her sentences.** Grammar spans in **red**, clickable for the rule in `EXPLAIN_LANGUAGE`
  (`en` default, or `ja`). Words clickable for a card: the reading, on'yomi and kun'yomi of each
  kanji, the English meaning, and — when it is on WaniKani — its SRS stage. A **translate icon**
  at the end of each sentence shows the English beneath it. Furigana per `FURIGANA`: `unknown`
  (default — kanji the student has not yet learned on WaniKani), `all`, or `off`. **Per kanji,
  not per run** (user, 2026-09-12): a compound with one unknown kanji used to carry one reading
  over the whole run; now a mixed run is split with WaniKani's own on'yomi/kun'yomi for each kanji
  (`WaniKaniProfile.kanji_readings`, rendaku and sokuon allowed), so the known ones hide and the
  unknown one keeps its reading. A run that cannot be split keeps one reading; `readings.txt`
  accepts dotted per-kanji corrections (`日本語 に.ほん.ご`) for the readings WaniKani does not list.
- **Reload replays the lesson** (user, 2026-09-12: a reloaded page sat on "she is thinking of how
  to start…" forever). The hub keeps this session's last 200 lines — hers with their marks,
  readings and words, yours as heard, an interrupted one flagged `cut`, never audio — and sends
  them as `history` after the `state` replay when a page connects; `state.spoken` says whether she
  has spoken in this lesson, so the opening caption is server truth, not page memory.
- **Tags, written by the tutor (prompt rules in `prompts/tutor.md`).** `{{span|point}}` wraps a
  grammar use — `span` is the text as it appears, `point` the grammar point's name, as Bunpro
  writes it where possible. `[target:point]` names what she wants the student to use next (a
  grammar point or a word); the page's **hint icon** shows it, closed until clicked, because the
  student should try first. Tags **never** reach TTS or the subtitle text: the chunker strips
  them and the sentence carries its spans. A malformed tag is dropped and logged, never spoken.
- **When the student gets it right** (user, 2026-09-12). `[used:word or point]` at the head of her
  reply — she is the one who can judge whether it was really theirs and really correct — floats
  that word up behind her in gold, the way her mood faces drift. Once per turn, never spoken, and
  recorded in the turn log so the habit can be measured.
- **Words** are cut and looked up on the orchestrator — tokenizer, WaniKani cache, offline
  dictionary — and sent with the sentence. No model call.
- **Explanations and translations** are asked for by the page and answered by a one-shot
  `claude -p` on the haiku tier under §4's rules (no tools, allowlisted environment, empty cwd),
  cached under `.cache/explain/`. They never block a turn and are never generated unasked.
- **Protocol additions** (generated like the rest, §8): `speak.grammar` (spans), `speak.vocab`
  (their own words, with reading, meaning and stage), `speak.readings` (furigana), `speak.target`,
  `speak.used` / `used_kind`, client `explain`, server `explanation`. Settings: `EXPLAIN_LANGUAGE`,
  `FURIGANA`, `STUDY_PANEL`.

## 9. STT DETAILS

- faster-whisper `large-v3`, `language="ja"`, `beam_size=5`, `condition_on_previous_text=False`, `vad_filter=False` (we run silero ourselves upstream).
- Silero VAD on the incoming stream; end-of-utterance = configurable silence (`VAD_SILENCE_MS`, default 900 ms — 600 ms cut a learner off mid-thought, 2026-09-10) after speech ≥ 300 ms. VAD emits `speech_start` / `speech_end` events; the orchestrator gates them by state — during `speaking`, the barge-in threshold and onset rules of §8 apply, so the avatar's own voice through the speakers does not end its turn.
- **DO** filter known Japanese Whisper hallucinations on silence/noise: discard results matching a blocklist (e.g. ご視聴ありがとうございました, おやすみなさい variants when energy was near-silence) and any transcript whose avg logprob / no-speech prob crosses thresholds. Make the blocklist a data file (`backend/data/hallucination_blocklist.txt`). **The blocklist rule** (2026-09-12): matching is punctuation- and whitespace-insensitive — words only, so 「ありがとう ございました。」 matches its entry and each decoration is not a new line; a transcript made of exactly **one** blocklisted phrase is rejected only when the audio was quiet (`STT_QUIET_RMS`, scaled to the room by `QUIET_OVER_FLOOR`) **or** the transcript is weakly predicted; **two or more** repeated blocklisted phrases are rejected at any level — Whisper's loops are never speech. A high `no_speech_prob` (`STT_MAX_NO_SPEECH_PROB`) likewise rejects only with a corroborating witness — quiet audio or `avg_logprob` below `STT_CORROBORATING_AVG_LOGPROB` — because its no-speech head is unreliable on utterances Silero has already trimmed; `STT_MIN_AVG_LOGPROB` alone is the floor.
- Warm the model at startup with a 1 s dummy transcription so the first real turn isn't slow.
- **Push-to-talk is the DEFAULT turn mode (user directive 2026-09-10).** `TURN_MODE=ptt|vad`.
  Under `ptt` the key/button holds the turn open and releasing it *is* `speech_end` — the silence
  window is not consulted at all, which removes this stage from the §10 budget entirely and makes
  the tutor's own voice a non-issue, so barge-in becomes explicit rather than inferred. The VAD
  still runs, because the level meter and the listening reactions (§8) read from it; it simply
  stops deciding when the turn ends. Both modes ship; `ptt` leads because it is simply more
  reliable — a key press is a statement of intent, where a silence window is a guess about one,
  and the guess is what produces both false end-of-turn and self-barge-in. Tuning `vad` well is
  deferred until the look and feel is settled (ROADMAP, deferred work).

### 9b. Discarding a bad capture — "no, let me say that again" (M3, user directive 2026-09-10)

A stutter, a cough, someone talking in the room: the capture is garbage and the student wants it
gone, not answered. A UI button or key binding (§8), not an automatic behaviour — only the student
knows their own sentence was wrong.

**The window matters, because the brain's session is append-only.** Once `brain.turn(text)` has
written to the subprocess stdin, that text is in the conversation for good; the CLI has no rewind,
and `--resume` replays the session *including* the garbage. So:

- **Before the send** — between `speech_end` and `brain.turn()` sits STT and the hallucination
  filter, typically a few hundred ms. Discarding here is free: drop the audio, return to
  `listening`, nothing was ever said. **This is the case to design for**, and the reason the
  transcript should be shown the instant it exists rather than only once the reply starts.
- **After the send** — cancelling now is the existing barge-in path, which stops the *reply* but
  cannot unsay the *prompt*. The tutor's context keeps the garbled line. Recovering properly means
  respawning with a new `--session-id` and replaying the good turns, which is the §9c reset below
  wearing a different hat, and costs a full restart of the conversation. Do not pretend a cheap
  undo exists here: either accept the polluted turn, or reset.
- **Push-to-talk makes this mostly moot**, which is a strong argument for it: releasing the key is
  the commit point, so an abandoned press (drag off the button, or a cancel key) throws the audio
  away before STT ever runs. The cancel key exists and is **ALT GR** — the right-hand ALT, since Chrome takes SPACE with the
  left one (user, 2026-09-12):
  `VoiceLoop.ptt_cancel()` empties the buffer and closes the hold, so the release that follows is a
  no-op and nothing is transcribed, let alone sent. In `vad` mode the commit happens on its own, which is exactly why the
  discard button is needed there.

### 9c. Starting over — clearing the whole conversation (M3)

Heavier sibling of §9b, for a lesson that has gone wrong rather than one bad sentence:

- A respawn with a **new** `--session-id` (§4) — explicitly *not* `--resume`, which is the
  crash-recovery path and would carry the ruined context back in.
- The student profile is **not** re-fetched. ADR-024 confines SRS calls to launch and manual
  Refresh, so the rendered prompt is rebuilt from the existing snapshot.
- Everything conversational resets: the chunker, the speech queue (cancel and re-arm — see the
  barge-in latch), the per-turn timings, the transcript panel. The status chips do not: VOICEVOX,
  Whisper and the SRS sync are not part of the conversation.
- The session log records the clear as an event rather than starting a new file, so an abandoned
  attempt is still minable afterwards (§15).

### Audio devices come and go (added 2026-09-10, user request)
A headset is unplugged mid-lesson, or is not there at launch and arrives later. Neither may end
the session, and neither may need a relaunch.
- **Missing at launch is not fatal.** The launch check warns and starts; the microphone thread
  waits (`missing`, retried every 2 s) and picks a device up the moment one appears.
- **Pulled out mid-session** (`PortAudioError` on read/write): the stream is closed and reopened
  on whatever is connected — the chosen device if it is back, else the system default.
- **Chosen device missing → system default**, reported as `fallback`; when it returns, the app
  switches back at the next idle moment, never while push-to-talk is held. "System default" is
  Windows' MME Sound Mapper where present, because it routes to the OS default device.
- **The list is watched from a child process** (`backend/device_watch.py`, every 2 s). PortAudio
  snapshots devices at initialisation, and re-initialising it in the app would kill the open
  microphone stream; a child with no streams can re-initialise freely. The settings panel's
  pickers refresh from it, and changing the input or output device there applies live.
- Every change is reported as `service_status: microphone` to the page and the terminal.
- Verified with a fake PortAudio (hermetic tests). **Not yet observed live:** an actual unplug and
  replug, and whether an open Sound Mapper stream follows a change of the OS default.

## 10. LATENCY BUDGET — HARD REQUIREMENT: ≤ 5.0 s voice→voice (instrument it — log per-turn timings as structured JSON)

| stage | budget |
|---|---|
| end-of-speech detect (VAD window) | 0.50 s |
| STT (whisper, warm) | 0.35 s |
| Claude first complete sentence (thinking included) | 3.60 s |
| VOICEVOX first chunk + WS delivery | 0.40 s |
| playback start slack | 0.15 s |
| **voice→voice total** | **≤ 5.0 s (p90)** |

**Changed 2026-09-10 by the user (ADR-033): the gate was ≤ 3.0 s.** The first 20-turn run
measured p50 3.49 s / p90 5.30 s, the Claude stage tracks the model's thinking, and thinking
cannot be turned off on Sonnet (constants.py). Every route under 3.0 s cost answer quality, and
the user chose quality: "5 s instead of 3 s for a high-quality answer is good".

Engineering this budget is a first-class requirement, not an afterthought:
- Claude stage is the variable one — enforce its allies: thinking kept down with `--effort medium` (it cannot be turned off on Sonnet, and lowering effort further trades away quality — ADR-033), compact system prompt (profile ≤ 600 tokens), tools stripped (§4), MCP tool use rationed (§6). Expose `--model` so the user can drop to a faster model if p90 drifts.
- Pipeline the stages: STT may start on VAD-provisional end-of-speech; TTS synthesis of sentence N overlaps Claude generating sentence N+1; ship the first sentence the instant it closes.
- Fillers (a short うーん、そうですね… from a pre-synthesised pool if the first sentence has not closed by 1.2 s) are the design of ADR-008 and remain it, but **they are not built — deferred to the end of the project** (2026-09-12): there is no `FILLER_AFTER_MS` key, no pool under `.cache/`, and no filler path in the voice loop. When they come, they count as masking, not as meeting the budget — true first-content latency is logged separately.
- **What is measured** (`backend/voice_loop.py` `TurnTiming`): `first_audio_ms` is when the first sentence's **synthesis** finished; `first_play_ms` is when its **playback started** — for the browser that is the hand-over to the page, the WebSocket hop is not included — and `first_play_ms` is the voice→voice number (until 2026-09-12 `first_audio_ms` was stamped as if it were).
- Stage timings are shown per turn: on the console (`backend/terminal.py` `timing_line`, and `/profile` in the text loop — a slash command; there is no `--profile` flag) and on the page (`timing`, with the rolling p50/p90 against the gate); log a warning with full breakdown whenever a turn exceeds 5.0 s (`LATENCY_WARN_S`), and a session report (nearest-rank percentiles, the same rule as `latency_run`) at exit. M3 acceptance includes: p90 ≤ 5.0 s over a 20-turn conversation, measured with `python -m backend.tools.latency_run`.

## 10b. VRAM BUDGET — HARD CAP: 8–10 GB on a 16 GB RTX-generation card

| component | allocation |
|---|---|
| faster-whisper `large-v3` @ `float16` | **3.8 GB measured** (V0.13) |
| Silero VAD | < 0.1 GB |
| CUDA context + fragmentation reserve | ~1 GB |
| browser/three.js (shares GPU) | ~1 GB |
| **total** | **~5.6 GB — comfortably inside cap** |

Rules:
- VOICEVOX stays CPU (Docker) — never move it to GPU.
- `make doctor` and the orchestrator's VRAM watch (`backend/vram.py`, `orchestrator.watch_vram`) report `nvidia-smi` memory use; log a warning above `VRAM_WARN_GB` (10 GB).
- If VRAM ever gets tight, step down in this order rather than breaching the cap: `large-v3` @ `int8_float16` (2.2 GB measured, costs accuracy — V0.13 heard 貯金 as ショッキング at int8 and got it right at fp16), then `medium`. Both are config flags, both are documented trade-offs.
- The remaining ~6 GB headroom is deliberately reserved for a future MuseTalk/photoreal experiment — do not spend it.

## 11. CONFIG, SECRETS, HYGIENE

- **Configuration is owned by a settings interface, not by `.env`.** The store is `settings.json` at the repo root (git-ignored, written atomically via temp-file + rename, mode `0600` where the OS has modes — on Windows the profile directory's ACLs are the protection and `chmod` is a no-op). `config.py` resolves, in order: built-in defaults → `settings.json` → `ATAMA_*` environment variables (optional overrides for automation/CI only — a normal user never touches them), and `config.load()` **validates** every value against the schema's type, choices and range, failing with a one-line `ConfigError` rather than starting on a bad value. Secrets (`WANIKANI_TOKEN`, `BUNPRO_API_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` — the schema's names) live in `settings.json` alongside everything else; that is the same trust class as a dotfile on a single-user laptop and no worse, with the OS keyring noted as a possible later upgrade, not a requirement.
- **The settings interface** is the frontend settings page (the M3 drawer grown up): every key in `config.py`, grouped (Account & tokens · Brain · Voice · Sound · Display · Advanced), with type-checked inputs, the default shown, and a description. Secrets use masked inputs; the server echoes secrets back **only** as `{set: true, hint: "…abcd"}` — the full value never leaves the backend once stored. "Does it work" is answered by the status chips (§5b) and by `make doctor`, not by per-service Test buttons on the page (none exist; the original design had them). Changes apply live where possible (the audio devices today; voice, VAD, display planned), trigger the documented respawn for `model`, and re-run the session-start fetch for tokens. **First run:** no keys → the first-run card of the next bullet; the lesson runs regardless (SRS is optional, and `claude` is checked by the doctor).
- **Keys added 2026-09-12** (all in the schema, all on the page): `VAD_SPEECH_THRESHOLD`, `VAD_ONSET_TOLERANCE_MS`, `STT_QUIET_RMS`, `QUIET_OVER_FLOOR`, `STT_MIN_AVG_LOGPROB`, `STT_MAX_NO_SPEECH_PROB`, `STT_CORROBORATING_AVG_LOGPROB` (§9), `PTT_HANDOVER_TIMEOUT_S` (releasing the key while the previous turn is still stopping), `VOICEVOX_TIMEOUT_S`.
- **There is no `.env`** (amended 2026-09-12, user: "we don't need the .env any more with the settings"). It was read after `settings.json` and before the environment, which made it a trap: a key set there could not be changed from the panel, and the panel had to explain why it was locked. `config.py`'s schema is now the whole inventory — every key carries its group and its one-line description, and the settings page is generated from it (a test asserts every key is presentable). `ATAMA_*` environment variables remain as the automation override. A leftover `.env` is **not read**: `config.stale_dotenv()` finds the keys still in it and the launch prints how to import them (`python -m backend.tools.migrate_env`, which now moves the tokens too — left behind they would simply stop working). **No `.env` exists for anything else either:** `SEARXNG_SECRET`, the one variable Docker Compose interpolates, is generated by the launcher (`backend/tools/up.py`) into the per-user state directory (`<claude-cwd's parent>/searxng-secret`, mode 0600 where supported) and passed to compose through the process environment — never a file in the repo, never one the user creates.
- **Nothing configured is a supported state, and it says so** (user, 2026-09-12). With no study keys the launch prints what is missing, where to add it (settings page → Account) and what happens meanwhile — the tutor teaches as if the student were an early beginner — plus `--no-srs` for a deliberately offline lesson. The page shows a first-run card built from the schema's unset secrets, with one button that opens Account and one that dismisses it for good; it disappears the moment a key is saved. The card names no service and no key: the read-only gate (§0) forbids the page naming them.
- `.gitignore` is part of the spec, not an afterthought. It ignores `.env*`, the generated `mcp.json`, `logs/`, `.cache/`, rendered prompts, `*.glb`, model weights, and `backend/tests/fixtures/private/`. `make doctor` verifies with `git check-ignore` that each of those paths is ignored, and fails otherwise.
- Generated, credential-bearing, or personal files all live under **`.cache/`** (ignored): `mcp.json`, the rendered prompts (`prompts/<session-id>.txt`), the WaniKani/Bunpro snapshots (`srs/`), the explanation cache (`explain/`), the learned compaction point. The claude subprocess cwd and the memory files live in the per-user state directory outside the repo (§4, §6b). Nothing generated at runtime is written next to source.
- `mcp.json` is generated directly by `backend/tools/mcp_config.py` from the resolved config at startup into `.cache/` (there is no template file; never commit credentials inside it). Credentials reach an MCP server only through that entry's `env` block — never through the claude process environment (§4 allowlist); today neither MCP server needs one (§5).
- Everything tunable lives in `config.py` reading env with sane defaults; no magic numbers scattered in code. Runtime changes from the settings drawer (§8) update the live config.
- Logs: `logs/session-<timestamp>.jsonl` — header record with the resolved config (secrets redacted) and the §5b status table; then every turn: user transcript, assistant text, timings, emotion(s), tool calls (name + sanitised args + ok/error). This is the user's future Anki mine; make it clean.
- **Redaction test.** A test writes a session with fake tokens set in the environment, then asserts that no configured secret value (and no `Bearer …` header) appears anywhere under `logs/` or `.cache/` except `.cache/mcp.json`. Tool-call args and error strings are the usual leak paths; sanitise at the logging boundary, not at each call site.
- `make check-secrets` — greps the tracked tree for token-shaped strings (Anthropic keys, `Bearer …`, Bunpro's `Token token=…` header echo, JWTs, and since 2026-09-12 the WaniKani token shape — a UUID v4 next to "wanikani"/"token" or inside an SRS fixture — and a `bunpro … token = <24+ chars>` assignment) and for every value currently in `settings.json`; it also flags a leftover `.env` at the repo root and scans its values. Wired into `make test`.
- `make check-readonly` — runs `backend/tools/readonly_gate.py` (§0). **First target of `make test`, and a prerequisite of `make run` and `make doctor`.** `make hooks` installs a pre-commit hook running both checks; `make doctor` warns if the hook is not installed.

## 12. MILESTONES — build strictly in order, each ends with a runnable demo + tests

**Order changed 2026-09-10 (user, ADR-034):** M2 declared done with M2a, M2b's silence check and
M2c's overlap and emotion checks deferred to the end of the project (not met); **M4 is built before M3**.

**M0 — Skeleton & environment doctor.** Repo layout below; **`backend/tools/readonly_gate.py` and the `make check-readonly` / `make hooks` targets first — the Golden Rule gate (§0) exists before any SRS code does, and `make test` cannot pass without it**; `.gitignore`, `config.py` with the defaults → `settings.json` → env resolution and the settings schema (§11), `backend/constants.py` with the verified CLI findings (§4); `backend/srs/http.py` read-only client (§5); `make doctor` checks: claude CLI present, version vs pinned, `ANTHROPIC_API_KEY` absent from the shell, a trivial `claude -p "respond with OK"` in stream-json with `init.apiKeySource == "none"` and result `OK`; CUDA visible; VOICEVOX reachable on loopback and not on other interfaces; tokens present; every ignored path actually ignored (`git check-ignore`). Prints the §5b status table. Clear actionable error messages. (The doctor was found missing on 2026-09-10 and written on 2026-09-12 — §4b lists what it checks today.)

**M1 — SRS fetchers (read-only) + text brain loop (no audio).** Built first, in this order, because they need no GPU or audio and the read-only guarantee must be proven before anything else touches the SRS accounts: (a) `backend/srs/http.py` GET-only client — **no setter, no write method, no `post`/`put`/`patch`/`delete`, nothing that could be extended into one without a new ADR**; (b) `srs/wanikani.py` and `srs/bunpro.py` fetchers with disk cache, on that client only; (c) `srs/profile.py` renderer (≤ 600 tokens); (d) `make doctor` extended with the scope reminder and, on `--live` only, one GET per token; then (e) the persistent claude subprocess wrapper + CLI REPL: type Japanese, see streamed sentence chunks with timings and per-sentence emotion, **with the real Student Profile already in the prompt**. Tests: read-only recording-transport test (every request in a full fetch of both sources is `GET`; the client has no write attribute) — this test exists from the first commit of `srs/http.py` and runs in every `make test` thereafter; fetcher tests against the V0.5 fixtures; cache TTL and `resync` tests; profile ≤ 600 tokens with zero/one/both sources; chunker unit tests (。！？, ellipses, emotion-tag stripping at turn start **and** sentence start, no mid-sentence splits); env-allowlist test (parent has `ANTHROPIC_API_KEY` and `WANIKANI_TOKEN`, child sees neither); `apiKeySource` assertion test; process restart/`--resume` test with the same `--session-id`; `rate_limit_event` handled without crashing; **verify that MCP tools survive `--tools ""`** (else fall back per §4).

**M2 — Ears & mouth (no avatar).** Mic → VAD → whisper → M1 loop → VOICEVOX → speaker playback in terminal/minimal page. Emotion → voice tone table live (§7). Tests: mora→viseme mapper golden tests against 3 recorded `audio_query` fixtures (commit fixtures); hallucination filter tests; emotion→VOICEVOX params test (each tag produces the configured style/pitch/speed/intonation; unknown style id degrades to base + scalars); VAD gating test (speech during `speaking` needs the higher threshold). Latency and VRAM instrumentation live from here.

**M3 — Face.** Full frontend with TalkingHead, viseme-synced speech, subtitles, emotions (face + voice, set at sentence playback start), listening reactions, idle life, status bar, settings drawer, barge-in end-to-end. Acceptance: 10-turn conversation on headphones where lip-sync looks tight and barge-in cuts speech < 300 ms; **10-turn conversation on laptop speakers with zero self-interruptions**; each of the four emotion tags visibly and audibly distinct in a scripted 4-sentence turn; p90 voice→voice ≤ 5.0 s over 20 turns (3.0 s until 2026-09-10, ADR-033); VRAM ≤ 10 GB steady state.

**M4 — Sensei brain.** Memory and context (§6b, ADR-031/032) — the turn log, the start-of-session read, the end-of-session summariser, and *last* the pre-emptive session rotation, which is the only piece here that can break a working conversation and so ships after several real lessons on the rest. `CONTEXT_ROTATE_AT` unset means never rotate, and that stays a supported configuration. Then: tutor prompt tuning with the real profile, Bunpro MCP (found or written per §5 — read tools only, on the same GET-only client), §5b status indicators wired to real state, `control: resync` from the UI. The fetchers and profile renderer already exist from M1; M4 is about the tutor *using* them well and the user *seeing* that it does. Acceptance: with a real WaniKani token, the tutor demonstrably uses ≥ 3 recent unlocks in a 5-minute conversation (visible in logs); Bunpro absent → clean degradation with the chip reading `disabled`; Bunpro MCP deliberately broken (bad credential) → chip reads `failed`, conversation unaffected; WaniKani offline with a warm cache → chip reads `stale`, profile still present.

**M5 — Polish.** Full settings page (§11: every key, grouped, masked secrets, first-run flow — `.env` is gone, amended 2026-09-12), session summary on goodbye, `--profile` overlay, README with setup for a fresh machine (Linux **and** Windows/WSL2 per §15), docker-compose for VOICEVOX on loopback, `make run`, `make check-secrets`, redaction test green, read-only GET-only test green. Acceptance: a fresh machine goes from clone to first conversation **without any `.env`**, entering tokens only through the settings page, and a launch with none entered says what is missing and what happens meanwhile.

## 13. REPO LAYOUT

```
atama-ai/                          (regenerated 2026-09-12 from `git ls-files backend frontend/src prompts docker`)
├─ backend/
│  ├─ app.py            # FastAPI + WS: Hub (fan-out, turn epochs, per-client outbox), Origin check
│  ├─ repl.py           # the CLI entry (`python -m backend.repl`)
│  ├─ orchestrator.py   # lesson wiring, rotation, resync, persona switch
│  ├─ page_control.py   # browser control dispatch (start/stop/cancel/resync/quit/new_topic/ready)
│  ├─ terminal.py       # console rendering
│  ├─ brain/            # Brain interface (ADR-027) + claude_cli.py provider (subprocess mgmt,
│  │                    #   stream-json, interrupt, resume, env allowlist, kill_tree)
│  ├─ voice_loop.py     # VAD/PTT → STT → brain → speaker; TurnTiming
│  ├─ audio.py          # device picker, playback, mic capture (PortAudio)
│  ├─ device_watch.py   # audio device list watched from a child process (§9)
│  ├─ speaker.py        # speech queue: synthesise N+1 while N plays
│  ├─ stt.py  vad.py  tts_voicevox.py  visemes.py
│  ├─ chunker.py        # sentence chunking + emotion / study tags (EMOTIONS)
│  ├─ emotions.py       # emotion → VOICEVOX style/params table (§7)
│  ├─ prompt.py         # prompt assembly with per-section budgets (§6)
│  ├─ memory.py  session.py  usage.py   # §6b: memory tiers, rotation, context meters
│  ├─ annotate.py  study.py  explain.py # §8b: furigana, their own words, explanations
│  ├─ study_plan.py     # §6c: today's targets, the coverage ledger, SRS spacing, openers, coach notes
│  ├─ model_tiers.py    # tier → model id (data in data/model_tiers.txt)
│  ├─ mcp_ready.py  search_mcp.py       # MCP readiness marker; the search MCP server (§5c)
│  ├─ status.py         # §5b service status registry → service_status messages
│  ├─ settings_view.py  # the settings echo: secrets as {set, hint}
│  ├─ vram.py           # nvidia-smi reading (§10b)
│  ├─ config.py  constants.py (verified CLI/endpoint findings, dated)
│  ├─ models.py (pydantic WS protocol → frontend/src/protocol.gen.ts)
│  ├─ srs/http.py       # GET-only client + ReadOnlyTransport — the runtime half of §0
│  ├─ srs/wanikani.py  srs/bunpro.py  srs/profile.py  srs/cache.py
│  ├─ srs/bunpro_mcp.py # stdio MCP server, three read tools, reads the snapshot
│  ├─ tools/readonly_gate.py  # §0 Golden Rule static gate — runs on every test/run/doctor/commit
│  ├─ tools/doctor.py   # `make doctor` (§4b)
│  ├─ tools/            # up, down, check_secrets, hooks, mcp_config, gen_protocol, latency_run,
│  │                    #   emotion_rate, stt_compare, voices, capture_*, make_fixtures, migrate_env,
│  │                    #   settings_cli, claude_probe, get_avatar, check_avatar, make_preview
│  ├─ data/             # hallucination_blocklist.txt, model_tiers.txt, news_feeds.txt, readings.txt,
│  │                    #   scenarios.txt (§6c openers)
│  └─ tests/            # fixtures/ (sanitised, committed)  fixtures/private/ (ignored)
├─ frontend/            # vite, vanilla TS
│  ├─ index.html  src/{main, ws, protocol.gen, avatar, audio_only, speech, expression, chat, rig,
│  │                     rigpanel, mic, status, settings, ui, talkinghead_pins}.ts (+ *.test.ts)
│  ├─ scripts/srs-grep.mjs  # prebuild/pretest half of the §0 gate
│  └─ public/<persona>.glb (git-ignored; README explains export)  cast.json, <persona>.speak.json (generated)
├─ prompts/             # tutor.md (template), tanaka|hayashi|minami|mori.md (personas),
│                       #   memory.md, handoff.md, summarise.md, summariser.md, coach.md (§6c)
├─ docker/searxng/settings.yml   # SearXNG config, committed, holds no secret
├─ .cache/              # git-ignored, created at startup: mcp.json, prompts/<session-id>.txt,
│                       #   srs/ snapshots, explain/, compaction.json
├─ logs/                # git-ignored: sessions/, latency/, stt/
├─ settings.json        # git-ignored, 0600 where the OS has modes, written by the settings page (§11)
├─ docker-compose.yml   # voicevox + searxng, both published on 127.0.0.1, images pinned
├─ .gitignore  Makefile  run.cmd  stop.cmd  pyproject.toml   (no .env; no mcp.json.template)
├─ README.md  ROADMAP.md  ADR.md  CLAUDE.md  ATAMA-AI_SPEC.md
```

## 14. GLOBAL DON'TS (Claude Code, read carefully)

- **DON'T** substitute the Anthropic API/SDK for the claude CLI subprocess — subscription auth via the CLI is a hard requirement.
- **DON'T** swap pinned stack pieces (no React, no VOICEVOX→cloud TTS, no whisper→cloud STT) without asking.
- **DON'T** fabricate CLI flags, endpoint schemas, or library method signatures. Verify (`claude --help`, hit VOICEVOX `/docs` OpenAPI locally, read TalkingHead README) and pin findings in code comments with date.
- **DON'T** commit secrets, the avatar GLB, model weights, or fixtures containing personal SRS data beyond the sanitized golden files.
- **DON'T** gold-plate: no auth system, no multi-user, no database. Files and one user.
- **DON'T** bind anything to `0.0.0.0`. Loopback only (§2).
- **DON'T** write to WaniKani or Bunpro. Ever. Read-only at token, client and tool level (§5). No exceptions, no "just this once", no write tools behind a flag.
- **DON'T** read a `.env`. The settings page is the configuration interface (§11); `ATAMA_*` env vars are an optional override for automation. **DON'T** send a stored secret back to the browser — `{set, hint}` only.
- **DON'T** pass the parent environment to the claude subprocess, spawn it from a directory containing a `CLAUDE.md`, or pass `--no-session-persistence` (§4).
- **DON'T** let an emotion tag reach TTS text, and **DON'T** let a sentence play without its emotion having reached both the face and the voice (§7, §8).
- **DO** ask me (the human) whenever a verification step fails or reality contradicts this spec — update the spec, don't silently diverge.

## 15. PLATFORM TOPOLOGY

**Linux (reference):** everything on the host. Browser, backend, Docker, `claude` login — one machine, one user, one home directory.

**Windows + WSL2:**
- **WSL2 side:** the Python backend (faster-whisper needs CUDA through the WSL2 driver — verify `nvidia-smi` works *inside* WSL2 before anything else), the `claude` CLI **and its login** (credentials live in the WSL home; a Windows-side `claude` login does not help the backend), `make`, Docker via Docker Desktop's WSL2 integration.
- **Windows side:** the browser only. WSL2 forwards `localhost` ports, so `http://localhost:5173` and the WS on `:8000` reach the WSL2 backend without further configuration — and because they are bound to loopback, they stay off the LAN.
- **Mic:** captured by the **orchestrator** (sounddevice, `backend/audio.py`), so under WSL2 the backend needs an audio device it can open — WSL2 has none by default, which is a real cost of this topology and is **not verified** on WSL2 (the user's machine is native Windows, below). The page only controls the turn (push-to-talk over the WS); playback is browser-side.
- **VOICEVOX:** Docker Desktop, same `docker-compose.yml`, same loopback binding.
- **Do not** split the backend across the two sides. The repo lives on the WSL2 filesystem (`~/...`, not `/mnt/c/...`) — CUDA model loading and file I/O are substantially slower through the `/mnt/c` bridge, which shows up directly in the latency budget.

`make doctor` reports which topology it detected and checks the WSL2-specific items above when applicable.

**Native Windows (the user's actual setup, discovered 2026-09-09):** Python 3.13 (Microsoft Store build), Node 22, Docker Desktop, a 16 GB RTX-generation laptop GPU. M0/M1 run unchanged. Two Windows-specific rules, both verified live: (1) the **Store Python virtualises `%LOCALAPPDATA%`** — directories it creates there are invisible to cmd.exe and to `claude`; the claude cwd therefore defaults to `~/.atama-ai/claude-cwd`, and no path shared with another process may live under AppData; (2) **spawn `claude` without a shell** (`shutil.which("claude")` → `claude.CMD`, executed directly). Whether the Store build can host faster-whisper + CUDA is decided at M2; switching to a python.org/uv-managed CPython is the expected outcome and is not a spec change.
