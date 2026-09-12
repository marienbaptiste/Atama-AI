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
4. `backend/srs/http.py` defines exactly one public client method, `get`, and the `ReadOnlyTransport` guard.
5. The MCP server module (if written) registers only the three read tools, and each tool's implementation calls only `get`.
6. No **code** file (`.py`, `.ts`, `.js`, `.json`, `.yaml`, `.toml`, `.sh`, `Makefile`) anywhere in the repo contains the WaniKani write scopes as strings (`assignments:start`, `reviews:create`, `study_materials:create`, `study_materials:update`, `user:update`) **except** the doctor's permission-warning check. Documentation may name them in order to tell the user to leave them unticked.
7. **SRS base URLs are constants, never settings.** `srs/http.py` hardcodes the WaniKani and Bunpro origins; no env var, setting, or function argument can change them (the survey of community servers showed a configurable base URL is exactly how a misconfiguration would ship a token to the wrong host). The gate fails on any `http://` / `https://` literal in `backend/srs/**` other than those origins, and on any base-URL parameter.

A violation prints the file, line, and rule, and exits non-zero. There is no `--skip`, no environment variable, no marker comment that bypasses it. If the gate itself is edited, the edit must be accompanied by a new ADR entry — and ADR-021 says that entry will not be written.

Everything in §5 and ADR-021 is the detailed form of this rule. If any other sentence in this document appears to permit a write, this section wins.

---

## 1. WHAT THIS IS

A desktop web app where the user has real-time spoken Japanese conversations with a 3D avatar tutor. The brain is Claude Code running headless (`claude -p`, subscription auth — NOT the API). The tutor knows the user's exact study state from WaniKani and Bunpro and adapts vocabulary/grammar accordingly. TTS is VOICEVOX (standard Tokyo-accent Japanese). The avatar is a Ready Player Me / Avaturn GLB rendered with the TalkingHead library, lip-synced from VOICEVOX mora timings.

Target machine: single laptop, RTX 4090 mobile (16 GB VRAM), Linux or Windows/WSL2. Everything runs locally except Claude inference.

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
It renders and plays, and sends `control` and `settings` messages. The **microphone is still
captured by the orchestrator** (`backend/audio.py`, sounddevice, with the device recovery of §9) —
browser mic streaming (data flow step 1, `audio_chunk`) is not built, and whether it should replace
the orchestrator's capture is an open question for the user. Memory is spec §6b, search §5c, the
launcher §15.

**A tutor is a persona and a voice together, from one file** (ADR-030). `prompts/<name>.md` holds
the character *and* declares the VOICEVOX style it is written for; `TUTOR_PERSONA` picks the file
and both follow. `VOICEVOX_SPEAKER=-1` (default) means "ask the persona"; a real id overrides it
for auditioning. Adding a tutor is adding one file — no Python, no config. Shipped: `tanaka`
(50, m), `hayashi` (28, m), `minami` (40s, f), `mori` (19, f — a conversation partner, not a
teacher).

Ports: orchestrator `:8000`, VOICEVOX `:50021`, frontend dev server `:5173` (or served statically by FastAPI).

**Bind to loopback only.** The WebSocket carries the raw mic stream and the student's SRS profile. uvicorn binds `127.0.0.1` (config `HOST`, default loopback — never `0.0.0.0`), Vite stays on localhost, and `docker-compose.yml` publishes VOICEVOX as `127.0.0.1:50021:50021`, not `50021:50021`. `make doctor` fails if any of the three is reachable on a non-loopback interface.

### Data flow per turn
1. Browser streams mic PCM (16 kHz mono) over WebSocket.
2. Silero VAD detects end of speech (~600 ms silence window, configurable).
3. faster-whisper transcribes (language="ja").
4. Transcript written as a user turn to the claude process stdin (stream-json).
5. Claude's streamed text is cut into sentences at 。！？…\n as tokens arrive.
6. Each sentence → VOICEVOX `audio_query` + `synthesis` → WAV + mora timings.
7. Orchestrator converts mora timings → Oculus viseme timeline, sends `{audio, visemes, vtimes, vdurations, text}` to browser.
8. Browser queues it into TalkingHead `speakAudio(...)`; avatar speaks with lip-sync.
9. If VAD fires while avatar is speaking → BARGE-IN: stop playback, flush TTS queue, mark remaining assistant text as undelivered, treat new speech as next turn.

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
  --system-prompt-file .cache/tutor_prompt_rendered.txt
```
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

**MCP readiness — wait for the signal, never sleep (verified live 2026-09-09).** Claude Code prints `init` *before* MCP servers connect (`mcp_servers: [{"name":"bunpro","status":"pending"}]`) and emits **no** "connected" event on stdout. If the first user turn is written while the server is pending, the model gets **no tools** and hallucinates. Our MCP server therefore announces readiness itself: on the client's `notifications/initialized` it writes the marker at `ATAMA_MCP_READY` (`{pid, connected_at}`; removed at shutdown; ~1.1 s after spawn). `claude_session.py` waits on that marker (timeout `CLAUDE_MCP_READY_TIMEOUT_S`, 20 s → status `bunpro_mcp = failed`, conversation continues without the tools) **before writing the first turn**. With the marker honoured, `--tools ""` yields `init.tools == [the three mcp__bunpro__* names]` and `mcp_servers: connected` — **V0.8 answered: `--tools ""` stands.** The `--disallowedTools` fallback is retired: built-in tool names vary by platform and context (a Windows run exposed `PowerShell`, `TaskCreate`… that the first probe never listed), so no static list is reliable.

**Child environment — allowlist, do not merely strip:** the child gets exactly `PATH`, `HOME` (`USERPROFILE`/`APPDATA`/`LOCALAPPDATA` on Windows), `TMPDIR`/`TEMP`, `LANG`, and `CLAUDE_CODE_OAUTH_TOKEN` if set. Nothing else. This is what keeps `WANIKANI_TOKEN` and Bunpro credentials out of the claude process and out of every MCP server it spawns. (Since the fetch policy of §5, the Bunpro MCP server reads the launch snapshot and needs **no credential at all**; its `mcp.json` `env` block carries only the snapshot path.) `ANTHROPIC_API_KEY` is therefore excluded by construction — and a unit test asserts it stays excluded even when set in the parent.

Verified `init` event fields (2.1.159): `session_id`, `model`, `tools[]`, `mcp_servers[]`, `apiKeySource`, `claude_code_version`, `cwd`, `permissionMode`. Verified `result` fields: `subtype`, `is_error`, `duration_ms`, `duration_api_ms`, `ttft_ms`, `num_turns`, `stop_reason`, `api_error_status`, `usage`, `modelUsage`, `total_cost_usd`, `session_id`. A `rate_limit_event` type exists. Per-entry shape of `mcp_servers[]` is still to be pinned with a real server configured (ROADMAP V0.2).

- **DO** write user turns to stdin as newline-delimited JSON: `{"type":"user","message":{"role":"user","content":[{"type":"text","text":"..."}]}}` — flush after each line.
- **DO** parse stdout line-by-line as JSON events. Handle at minimum: the `init` event (assert `session_id` matches the one we passed; read `apiKeySource`, `tools`, `mcp_servers`), streamed partial text deltas, complete assistant messages, tool-use events and their `tool_result` blocks (surface as "thinking" state to the UI, and feed the Bunpro MCP status indicator — §5b), `rate_limit_event` (surface in the status bar; the fallback model handles continuation), and the `result` event per turn (log `ttft_ms` and `duration_api_ms` into the turn timing record — they are the Claude stage's ground truth). Log and skip unknown event types — do not crash on them.
- **DO** assert `apiKeySource == "none"` on `init`. Anything else means an API key reached the child and the session is billing the API: refuse to continue, surface the error, and stop.
- **DO** restart with `--resume <session_id>` (same cwd, same env) if the process dies, so conversation memory survives crashes.
- **DO** strip a leading emotion tag `[happy]|[thinking]|[surprised]|[serious]` from assistant text before TTS; forward it to the frontend as an `emotion` message.
- **DON'T** spawn a process per turn (startup latency kills the experience).
- **DON'T** use `--bare` — bare mode does not read `CLAUDE_CODE_OAUTH_TOKEN`.
- **DON'T** pass the parent environment through. Allowlist (above). `ANTHROPIC_API_KEY` silently overrides subscription auth and bills the API; SRS tokens have no business in the child.
- **DON'T** spawn from the repo root or any directory containing a `CLAUDE.md`.
- **DON'T** use `--dangerously-skip-permissions`. The tutor needs no file/exec tools at all; `--tools ""` both removes risk and shrinks the prompt.
- **DON'T** let a wedged turn hang the app: per-turn timeout (default 60 s) → send SIGINT to the process, surface an apology line, restart with `--resume` if needed.

### 4b. Claude login flow (what the user does, once)

Two supported paths; `make doctor` explains which is active.

1. **Interactive login (default).** Run `claude` once in a terminal **on the machine that will run the backend** (for Windows/WSL2 that means inside WSL2 — credentials live in the WSL home, not the Windows one), then `/login`. The CLI opens a browser or, when it cannot, prints a URL to paste. Credentials are stored under `~/.claude/` outside the repo and refresh automatically. Headless `claude -p` reuses them with no further setup.
2. **Long-lived token.** `claude setup-token` ("Set up a long-lived authentication token (requires Claude subscription)") prints a token; paste it into the settings page as the Claude OAuth token (`CLAUDE_CODE_OAUTH_TOKEN`). Interactive — the user runs it, never `make`. Use this when the backend runs somewhere the interactive login is awkward.

`make doctor` checks, in order: `claude` on PATH and its version (warn if it differs from the pinned verified version); `ANTHROPIC_API_KEY` **absent** from the shell that runs `make` (hard fail if present — it will leak into everything); a trivial `claude -p "respond with OK" --output-format stream-json` whose `init.apiKeySource == "none"` and whose `result.result` is `OK`. That is the only reliable "you are on subscription, not API billing" proof available.

**Subscription rate limits are a real failure mode** for a chatty voice app. On `rate_limit_event`: surface it in the status bar, let `--fallback-model` carry the conversation, and log it. If a turn fails outright with `api_error_status`, speak an apology line and keep the session alive.

## 5. SRS INTEGRATION (WaniKani + Bunpro)

**HARD RULE — READ-ONLY (the detailed form of the Golden Rule, §0). This application never writes to WaniKani or Bunpro.** No starting assignments, no submitting reviews, no creating study materials, no updating user settings, no marking anything. Not from the orchestrator, not from the MCP server, not from Claude. Enforced at three layers, all required:
1. **Token scope.** The WaniKani personal access token is created with **no write permissions checked** (leave `assignments:start`, `reviews:create`, `study_materials:create`, `study_materials:update`, `user:update` unticked). `make doctor` fetches `/v2/user` and warns if the token reports any write permission. Bunpro's API is unofficial and unscoped, so layers 2 and 3 are the whole defence there.
2. **Client construction.** The SRS HTTP client (`backend/srs/http.py`, shared by both fetchers and by the Bunpro MCP server) exposes **only** a `get()` method. There is no `post`/`put`/`patch`/`delete` to call by accident, and a test asserts a non-GET method is not reachable through it.
3. **Tool surface.** The Bunpro MCP server exposes read tools only (`get_review_queue`, `get_ghost_reviews`, `get_grammar_progress`). If V0.7 picks a community server that has write tools, it is disqualified unless those tools can be removed from the surface — `--allowedTools` with an explicit read-only MCP tool list is the belt-and-braces; a prompt instruction is **not** sufficient.

A test in the standing suite records every outgoing HTTP request during a full mocked session (including MCP tool calls) and asserts all are `GET`.

At session start (parallel, 10 s total budget, both OPTIONAL — the app must run fine with zero, one, or both configured):
- **WaniKani** (official, stable): `GET https://api.wanikani.com/v2/user` and `GET /v2/assignments?...` with `Authorization: Bearer $WANIKANI_TOKEN`. Extract: level, count of items by SRS stage, ~30 most recently unlocked vocab (kanji + reading + meaning), ~15 leeches (low-stage, high-incorrect items) if derivable. Respect the ~60 req/min rate limit; cache to disk with 1 h TTL.
- **Bunpro** (unofficial — treat as fragile): via MCP server configured in `mcp.json` so Claude can query it live mid-conversation, AND a session-start fetch of JLPT progress + ~15 recent/ghost grammar points for the static profile. Wrap every Bunpro call in try/except; on any failure log a warning and continue without it. Never let Bunpro breakage block startup.

**The Bunpro MCP server: we write it (ROADMAP V0.7, decided 2026-09-09; ADR-023).** Bunpro has **no official API** — it was deprecated in 2024 and the docs removed; reverse-engineering the site's `/api/frontend/*` endpoints is permitted by staff with the warning that they "may change without warning". Three community MCP servers exist: one has write tools (disqualified by §0), one needs the user's email + password (disqualified — this app never holds a password), one is read-only but stats-shaped and built for hosted deployment. So: `backend/srs/bunpro_mcp.py`, a small stdio MCP server exposing exactly `get_review_queue`, `get_ghost_reviews`, `get_grammar_progress`, on the same GET-only client as the session-start fetch.

**Fetch policy — never spam the SRS APIs (user directive 2026-09-09, ADR-024).** WaniKani and Bunpro are contacted **only at app launch and on the student's manual Refresh** (`control: resync`). Nothing else ever calls them — not a timer, not a turn, not an MCP tool. Each launch/refresh stores a **snapshot** (`.cache/srs/<service>.json` with `fetched_at`) and the status registry persists to `.cache/srs/status.json`. A launch re-uses the snapshot if it is younger than `SRS_CACHE_TTL_S` (default 10 min — a guard against hammering the APIs on rapid restarts), otherwise fetches once. **The Bunpro MCP server makes no network requests at all**: its tools read the snapshot and every answer carries `synced_at` / `age_minutes`, so the tutor can say "as of your last sync". Consequently the MCP server needs **no token** — nothing secret enters that process, and `mcp.json` contains no credential. Measured 2026-09-09: launch fetch of both sources 4.6 s (budget 10 s), 6 Bunpro calls + 5 WaniKani calls.

**Bunpro credential:** the **Account API Token from Bunpro → Settings → API**, sent as `Authorization: Token token=<token>` **together with the query parameter `dangerously_authenticate_using_api_token=true`** and browser-like `Origin`/`Referer` headers of `https://bunpro.jp` — verified live 2026-09-09: without the parameter every endpoint returns 401 `AUTH_USER_DENIED`. (Bunpro's own naming flags this token as a full-account credential; the read-only transport is the mitigation.) `srs_level_details` takes a **named** `level` (`beginner|adept|seasoned|expert|master`); a numeric or missing level returns HTTP 500. That is the whole credential story — no email, no password, no browser-cookie scraping. Base origin `https://api.bunpro.jp` (what the current read-only community server hardcodes; confirm at M1, then it is a constant in `srs/http.py` — never a setting, §0 rule 7). Read endpoints: `/user`, `/user/due`, `/user/queue`, `/user_stats/jlpt_progress_mixed`, `/user_stats/srs_level_details?reviewable_type=Grammar`, `/user_stats/srs_ghost_level_details?reviewable_type=Grammar`, `/user_stats/forecast_daily`. Politeness: ≥ 1 s between requests (2 s × the 5 session-start calls would not fit the 10 s budget — measured 2026-09-09), no aggressive retry after a 429. Every response is validated against a pinned fixture so an upstream change fails loudly into the `bunpro_mcp` status chip instead of silently feeding garbage to the tutor. The server is launched by the claude subprocess via `mcp.json` and receives the token only through that entry's `env` block.

Render the collected data into a compact **Student Profile** (≤ 600 tokens) that is inserted into the tutor system prompt template below. Also write it to `logs/profile-<date>.json` for debugging.

### 5b. Service status indicators (SRS sync + MCP health)

The UI must show, at all times, whether each external dependency is actually working — not just configured. One compact status bar (chips, collapsible into the debug panel) driven by a `service_status` WS message per service, sent on every state change and at least every 30 s.

| service        | states                                                                                   | source of truth |
|----------------|------------------------------------------------------------------------------------------|-----------------|
| `wanikani`     | `disabled` (no token) · `syncing` · `ok` (level N, synced HH:MM) · `stale` (serving cache; last fetch failed) · `error` (no cache, fetch failed) | the fetcher itself + cache timestamps |
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

Boundaries:

- **A separate MCP server** — `backend/search_mcp.py`, exposing one read tool, `search`. It is **never** a fourth tool on the Bunpro server, whose exact three-tool surface the Golden Rule gate asserts (§0 rule 5).
- **Rationed by the prompt (§6):** search at the start of a session to find something worth discussing, and later only when the conversation genuinely needs a fact. Not every turn.
- **The cost lands where it is affordable.** The opening search happens before the student has spoken, where a second or two is invisible. The 5.0 s voice→voice budget (§10) governs conversational turns; any turn containing a tool call logs its tool time separately so the p90 measurement stays honest.
- **Results are untrusted text.** Titles and short snippets only, never full pages: control characters and `[`/`]` stripped (they would collide with the emotion tags of ADR-020), length-capped, count-capped. The tutor holds no built-in tools (§4), so a hostile result can at worst make her say something odd.
- **Optional, degrades cleanly.** No SearxNG reachable → the tool says so, the `search` chip reads `down`, and Sensei opens from the student's profile and the previous session instead. Startup never blocks on it. There is deliberately **no static topic list** — the fallback is her own curiosity, not a canned menu.

**Status: implemented and verified live** (2026-09-09, and multi-source 2026-09-10). This section previously said "not implemented"; that was stale — the MCP server has been answering in live sessions since M1.

**Sources are interleaved, not ranked by one engine** (2026-09-10, user directive). Measured on a Japanese news query, 47 of 57 SearxNG results came from `brave.news` alone, so taking the first few gave the tutor one source in practice. `news` results now draw round-robin across providers and are de-duplicated by title. Two SearxNG engines that were on by default are disabled in `docker/searxng/settings.yml` — `google news` (suspended behind a CAPTCHA) and `startpage news` (parse error): they returned nothing and each cost up to the request timeout on every search; disabling them took a search from several seconds to 0.7 s.

**Yahoo! JAPAN ニュース, through its official RSS** — not SearxNG, whose only Yahoo news engine is the English US site and is not even defined in the default news set. Nine topic feeds (top picks, domestic, world, business, entertainment, sports, IT, science, local) are listed in `backend/data/news_feeds.txt`, merged into `news` results only, and counted as **one provider** so nine feeds cannot fill every slot. Headlines older than 72 hours, or undated, are dropped. The tool is still one read tool, `search`; the feeds are sources behind it, not a second tool.

**NHK is deliberately excluded.** `https://www3.nhk.or.jp/rss/news/cat<N>.xml` still answers 200, but on 2026-09-10 every item across cat0/1/5/6 was 32–38 days old and `lastBuildDate` was 8–9 August: the feed is frozen (NHK moved to `news.web.nhk`). A 200 with month-old items is worse than a 404, because the tutor would present stale news as current — which is exactly what the age cutoff exists to prevent. NHK **News Web Easy** remains unusable: `news-list.json` returns `401 missing_token`.

## 6. TUTOR SYSTEM PROMPT (template — render with soul, profile and topic; versioned files under `prompts/`)

The prompt is assembled from **three** versioned, user-editable files and two runtime values, in this order — persona first, hard rules last, so the rules win by position:

| Placeholder | Source | Cap |
|-------------|--------|-----|
| `{{soul}}` | **`prompts/soul.md`** — who Sensei is: background, life, manner (ADR-026). Optional; absent → a neutral competent tutor. Persona may colour *how* she speaks, never override the HARD OUTPUT RULES below. | ≤ 400 tokens |
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
- Begin the turn, and optionally any later sentence, with exactly one emotion tag from: [happy] [thinking] [surprised] [serious]. The tag drives your face and your voice tone, so choose it for how that sentence should SOUND: [happy] for praise and warmth, [thinking] when working something out or asking them to try again, [surprised] for a genuinely good answer or an unexpected turn, [serious] for a correction that matters. No tag means neutral. Nothing else in brackets, ever.

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
| turn log | `logs/sessions/<date>-<session>.jsonl` | never by the tutor | appended in the speaking gap | — |
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
  through. A session is "summarised" when `topics.jsonl` holds a row for it, so there is no second
  bookkeeping file to drift.
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
 "latency": {"ttft_ms", "first_audio_ms", "voice_to_voice_ms"},
 "usage":   {...as the provider reported it...}}
```

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
  sections plus the brief.
- **Swap at a turn boundary, never inside one.** Not ready when the student speaks? Keep the old
  process and try the next gap. Rotation is never the reason a turn is slow.
- If rotation keeps failing, let the provider compact and **log it as a latency event** so §10
  instrumentation shows it for what it is.
- `--resume` (§4) resumes whichever session is authoritative — the old one, until the swap lands.

**Tool output is summarised before it enters context.** A search result set or an SRS snapshot
pasted whole is thousands of tokens re-read on every subsequent turn for the rest of the session.
Capping it is cheaper than rotating more often.

## 7. TTS + LIP-SYNC (VOICEVOX → Oculus visemes)

- `POST /audio_query?text=<sentence>&speaker=<id>` → JSON with `accent_phrases[].moras[]` (each mora: `consonant`, `consonant_length`, `vowel`, `vowel_length`) plus `pause_mora`, `prePhonemeLength`, `postPhonemeLength`, `speedScale`.
- `POST /synthesis?speaker=<id>` with that JSON → WAV bytes.
- Build the viseme timeline by walking morae and accumulating time. Honor `prePhonemeLength` offset and `speedScale` (divide durations by it). Timing unit: output milliseconds relative to audio start (TalkingHead's `vtimes`/`vdurations` convention — confirm ms vs s against its README and pin).
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
  Enumerate the installed speaker's real style ids from `GET /speakers` at startup (**V0.3** pins the endpoint shape); if a mapped style id does not exist, log a warning and use the base style with the scalar overrides only.
  For a speaker with **only one style** (麒ヶ島宗麟, 栗田まろん, 冥鳴ひまり …) every emotion lands on the same voice, so the scalars are widened to keep the emotions apart — but **pitch only** (`SINGLE_STYLE_PITCH_SPREAD` 1.8), never intonation (`SINGLE_STYLE_INTONATION_SPREAD` 1.0). Widening `intonationScale` stretches the model's own F0 wobble along with the contour: measured 2.15% -> 2.41% jitter at 1.8x, audible as an unsteady voice between phonemes. `pitchScale` is a constant offset in log-F0 and carries the emotion at no stability cost. Numbers above are starting points — tune by ear in M3. The emotion applies to the sentence carrying the tag and every following sentence until the next tag or the end of the turn.
- **DO** synthesize sentence-by-sentence as chunks arrive from Claude; **DON'T** wait for the full reply.
- **DON'T** send text to TalkingHead's `speakText` — its text lip-sync has no Japanese module. ALWAYS use `speakAudio` with the audio + viseme timeline.

## 8. FRONTEND

- TalkingHead init with the GLB avatar, lipsyncModules can be empty (we always pass visemes explicitly).
- WebSocket client with auto-reconnect. Message protocol (define as typed constants shared in one place, mirrored in Python pydantic models; a contract test asserts the two sets are identical):
  - client→server: `audio_chunk` (base64 PCM16), `control` (`start`, `stop`, `cancel`, `bargein_ack`, `resync`, `quit` — `start`/`stop` are the push-to-talk edges, and `cancel` is **ALT GR during a hold** (the right-hand ALT: Chrome claims SPACE with the left one): what has been recorded is dropped, the talk key can then be released without sending anything, and the next press starts clean (user, 2026-09-12); `quit` shuts the orchestrator down cleanly from the page, and is deliberately not `stop`; `new_topic` is the page's New topic button — she drops the subject, interrupting herself if need be, and searches for a fresh one exactly as if the student had said 「話題を変えて」), `settings` (partial update of **any** key in the `config.py` schema, secrets included — §11; the server validates, persists to `settings.json`, applies live where possible, and replies with the applied `settings` echo in which secrets appear only as `{set, hint}`. `model` takes effect by respawning the claude subprocess with `--resume`, reported via `service_status: claude=restarting`; token changes re-run the session-start SRS fetch), `settings_test` (`{service}` — runs that service's real check and reports through `service_status`).
  - server→client: `state` (`listening|thinking|speaking`), `stt_final`, `assistant_text` (for subtitle display), `speak` (`{audio_b64, visemes[], vtimes[], vdurations[], text, emotion}`), `emotion`, `bargein`, `srs_profile` (for a collapsible debug panel), `service_status` (§5b), `settings` (echo), `timing` (per-turn stage breakdown, §10), `error`. `stt_partial` is **reserved**: STT runs on complete utterances, so partials are not produced in M2–M5; the type exists so a streaming-STT experiment does not need a protocol change.
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

  Moods crossfade over 300 ms. Where TalkingHead has no matching mood (surprised, serious), drive ARKit blendshapes directly via its morph-override facility — the exact API is pinned in V0.4, not assumed. Emotion is set **when the sentence carrying the tag starts playing**, not when the tag is parsed — otherwise the face reacts a full TTS latency before the voice does. The `speak` message therefore carries the sentence's `emotion` alongside its audio.
- Barge-in on the client: on local VAD-ish energy spike while `speaking`, immediately pause audio and notify server; server confirms with `bargein`.
- **Self-barge-in (the avatar interrupting itself).** The mic hears the speakers. Three layers, all required: (1) `getUserMedia` with `echoCancellation: true`, `noiseSuppression: true`, `autoGainControl: false`; (2) while `speaking`, both the client energy check and the server VAD use a **higher threshold** (config `BARGEIN_THRESHOLD_FACTOR`, default 2.0 — note the server VAD emits a *probability* capped at 1.0, so the factor divides the headroom to certainty, `1-(1-t)/f`, giving 0.75 at t=0.5: multiplying would put the bar at 1.0 and silently disable barge-in, verified 2026-09-09) and require ≥ 250 ms of sustained speech before firing; (3) the server ignores VAD `speech_start` events during the first 150 ms of playback (loudspeaker onset). M3 acceptance includes a **10-turn conversation on laptop speakers with zero self-interruptions**, in addition to the headphones run.
- Mic capture via AudioWorklet at 16 kHz mono PCM16 (NOT MediaRecorder/opus — we want raw frames for server VAD).
- **DON'T** add build complexity: no React, no state library. One page, a few modules.

## 8b. STUDY PANEL (ADR-036 — user request 2026-09-11. Built 2026-09-12: the layout, the chat, the tutor's marks, red grammar, the hint, furigana, the word you used floating behind her. Explanations and translations on click, 2026-09-13. Not yet: word cards)

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
- **Blue is one of their own words** (user, 2026-09-12): **every** vocabulary item the student has
  not yet Guru'd (WaniKani stage < 5 — not only the newest thirty, which left her a palette of 21
  words and 13 % of her sentences carrying one) and the grammar they have not mastered, from the
  snapshot already on disk —
  `backend/study.py`, no model call and no fetch (ADR-024). It marks her sentences and their own,
  longest match first; where a word sits inside a grammar point the red wins, because two nested
  marks are a box inside a box. The same lists decide the **float behind her**: `[used:…]` is
  looked up, `speak.used_kind` says whether it was one of their words or one of their grammar
  points, and it drifts up in that colour. The student's own turn floats one too, the moment the
  transcript arrives: that one is objective (the word is theirs and they said it), while her credit
  is the only one that can be a grammar point. A word matches as written, as its kana reading, and
  by the prefix its inflections share — gold when the tutor credits something from neither
  list, which is also how a tutor crediting the wrong thing shows up.
- **Red is grammar, and only grammar** (user, 2026-09-12). A conjugation, an auxiliary or a
  pattern, wrapped whole — 〜てみよう is marked from the stem, not from its tail. A noun, a plain
  verb or adjective, a name or a number is vocabulary and is never red, whatever the tutor thinks
  of the word. The prompt says so, and `Annotator.grammar_only` enforces it with the tokenizer
  already loaded for furigana: a span whose every token is a noun, with a point not named as a
  pattern, is dropped before the page sees it. Narrow on purpose — a dropped point costs more than
  a stray word.
- **Her sentences.** Grammar spans in **red**, clickable for the rule in `EXPLAIN_LANGUAGE`
  (`en` default, or `ja`). Words clickable for a card: the reading, on'yomi and kun'yomi of each
  kanji, the English meaning, and — when it is on WaniKani — its SRS stage. A **translate icon**
  at the end of each sentence shows the English beneath it. Furigana per `FURIGANA`: `unknown`
  (default — kanji the student has not yet learned on WaniKani), `all`, or `off`.
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
- **Protocol additions** (generated like the rest, §8): `speak.grammar` (spans), `speak.words`,
  `speak.target`, client `explain`, server `explanation`. Settings: `EXPLAIN_LANGUAGE`,
  `FURIGANA`, `STUDY_PANEL`.

## 9. STT DETAILS

- faster-whisper `large-v3`, `language="ja"`, `beam_size=5`, `condition_on_previous_text=False`, `vad_filter=False` (we run silero ourselves upstream).
- Silero VAD on the incoming stream; end-of-utterance = configurable silence (`VAD_SILENCE_MS`, default 900 ms — 600 ms cut a learner off mid-thought, 2026-09-10) after speech ≥ 300 ms. VAD emits `speech_start` / `speech_end` events; the orchestrator gates them by state — during `speaking`, the barge-in threshold and onset rules of §8 apply, so the avatar's own voice through the speakers does not end its turn.
- **DO** filter known Japanese Whisper hallucinations on silence/noise: discard results matching a blocklist (e.g. ご視聴ありがとうございました, おやすみなさい variants when energy was near-silence) and any transcript whose avg logprob / no-speech prob crosses thresholds. Make the blocklist a data file.
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
- If the first sentence hasn't closed by 1.2 s of streaming, emit a short filler (うーん、そうですね…) from a pre-synthesized filler pool while generation continues. Fillers count as masking, not as meeting the budget — log true first-content latency separately.
- `--profile` flag / debug overlay shows last-turn stage timings; log a warning with full breakdown whenever a turn exceeds 5.0 s (`LATENCY_WARN_S`), and track rolling p50/p90 in the session log. M3 acceptance includes: p90 ≤ 5.0 s over a 20-turn conversation, measured with `python -m backend.tools.latency_run`.

## 10b. VRAM BUDGET — HARD CAP: 8–10 GB on the RTX 4090 mobile (16 GB card)

| component | allocation |
|---|---|
| faster-whisper `large-v3` @ `float16` | **3.8 GB measured** (V0.13) |
| Silero VAD | < 0.1 GB |
| CUDA context + fragmentation reserve | ~1 GB |
| browser/three.js (shares GPU) | ~1 GB |
| **total** | **~5.6 GB — comfortably inside cap** |

Rules:
- VOICEVOX stays CPU (Docker) — never move it to GPU.
- `make doctor` and the `--profile` overlay report `nvidia-smi` memory use; log a warning above 10 GB.
- If VRAM ever gets tight, step down in this order rather than breaching the cap: `large-v3` @ `int8_float16` (2.2 GB measured, costs accuracy — V0.13 heard 貯金 as ショッキング at int8 and got it right at fp16), then `medium`. Both are config flags, both are documented trade-offs.
- The remaining ~6 GB headroom is deliberately reserved for a future MuseTalk/photoreal experiment — do not spend it.

## 11. CONFIG, SECRETS, HYGIENE

- **Configuration is owned by a settings interface, not by `.env`.** The store is `settings.json` at the repo root (git-ignored, created with mode `0600`, written atomically via temp-file + rename). `config.py` resolves, in order: built-in defaults → `settings.json` → environment variables (optional overrides for automation/CI only — a normal user never touches them). Secrets (`wanikani_token`, `bunpro_api_key`, `claude_oauth_token`) live in `settings.json` alongside everything else; that is the same trust class as `.env` on a single-user laptop and no worse, with the OS keyring noted as a possible later upgrade, not a requirement.
- **The settings interface** is the frontend settings page (the M3 drawer grown up): every key in `config.py`, grouped (Account & tokens · Voice · Speech detection · Model · Display · Advanced), with type-checked inputs, the default shown, and a description. Secrets use masked inputs; the server echoes secrets back **only** as `{set: true, hint: "…abcd"}` — the full value never leaves the backend once stored. Each external-service group has a **Test** button that runs the real check (WaniKani `/v2/user` with the read-only warning, Bunpro fetch, VOICEVOX `/version`, `claude` trivial prompt with the `apiKeySource` assertion) and reports through the §5b status registry, so "does it work" and "is it configured" are the same screen. Changes apply live where possible (voice, VAD, display), trigger the documented respawn for `model`, and re-run the session-start fetch for tokens. **First run:** no `settings.json` → the app opens on the settings page with only the Test buttons active, and the conversation UI unlocks once `claude` tests green (SRS remain optional).
- **There is no `.env`** (amended 2026-09-12, user: "we don't need the .env any more with the settings"). It was read after `settings.json` and before the environment, which made it a trap: a key set there could not be changed from the panel, and the panel had to explain why it was locked. `config.py`'s schema is now the whole inventory — every key carries its group and its one-line description, and the settings page is generated from it (a test asserts every key is presentable). `ATAMA_*` environment variables remain as the automation override. A leftover `.env` is **not read**: `config.stale_dotenv()` finds the keys still in it and the launch prints how to import them (`python -m backend.tools.migrate_env`, which now moves the tokens too — left behind they would simply stop working). Docker Compose keeps its own `.env` for `SEARXNG_SECRET`; that is compose's mechanism, not this app's.
- **Nothing configured is a supported state, and it says so** (user, 2026-09-12). With no study keys the launch prints what is missing, where to add it (settings page → Account) and what happens meanwhile — the tutor teaches as if the student were an early beginner — plus `--no-srs` for a deliberately offline lesson. The page shows a first-run card built from the schema's unset secrets, with one button that opens Account and one that dismisses it for good; it disappears the moment a key is saved. The card names no service and no key: the read-only gate (§0) forbids the page naming them.
- `.gitignore` is part of the spec, not an afterthought. It ignores `.env*`, the generated `mcp.json`, `logs/`, `.cache/`, rendered prompts, `*.glb`, model weights, and `backend/tests/fixtures/private/`. `make doctor` verifies with `git check-ignore` that each of those paths is ignored, and fails otherwise.
- Generated, credential-bearing, or personal files all live under **`.cache/`** (ignored): `mcp.json`, `tutor_prompt_rendered.txt`, the claude subprocess cwd, WaniKani/Bunpro caches, the pre-synthesised filler pool. Nothing generated at runtime is written next to source.
- `mcp.json` generated from `mcp.json.template` + the resolved config at startup into `.cache/` (never commit credentials inside it). Credentials reach an MCP server only through that entry's `env` block — never through the claude process environment (§4 allowlist).
- Everything tunable lives in `config.py` reading env with sane defaults; no magic numbers scattered in code. Runtime changes from the settings drawer (§8) update the live config.
- Logs: `logs/session-<timestamp>.jsonl` — header record with the resolved config (secrets redacted) and the §5b status table; then every turn: user transcript, assistant text, timings, emotion(s), tool calls (name + sanitised args + ok/error). This is the user's future Anki mine; make it clean.
- **Redaction test.** A test writes a session with fake tokens set in the environment, then asserts that no configured secret value (and no `Bearer …` header) appears anywhere under `logs/` or `.cache/` except `.cache/mcp.json`. Tool-call args and error strings are the usual leak paths; sanitise at the logging boundary, not at each call site.
- `make check-secrets` — greps the tracked tree for token-shaped strings and for every value currently in `settings.json` (and in any leftover `.env`); wired into `make test`.
- `make check-readonly` — runs `backend/tools/readonly_gate.py` (§0). **First target of `make test`, and a prerequisite of `make run` and `make doctor`.** `make hooks` installs a pre-commit hook running both checks; `make doctor` warns if the hook is not installed.

## 12. MILESTONES — build strictly in order, each ends with a runnable demo + tests

**Order changed 2026-09-10 (user, ADR-034):** M2 declared done with M2a, M2b's silence check and
M2c's overlap and emotion checks deferred to the end of the project (not met); **M4 is built before M3**.

**M0 — Skeleton & environment doctor.** Repo layout below; **`backend/tools/readonly_gate.py` and the `make check-readonly` / `make hooks` targets first — the Golden Rule gate (§0) exists before any SRS code does, and `make test` cannot pass without it**; `.gitignore`, `config.py` with the defaults → `settings.json` → env resolution and the settings schema (§11), `backend/constants.py` with the verified CLI findings (§4); `backend/srs/http.py` read-only client (§5); `make doctor` checks: claude CLI present, version vs pinned, `ANTHROPIC_API_KEY` absent from the shell, a trivial `claude -p "respond with OK"` in stream-json with `init.apiKeySource == "none"` and result `OK`; CUDA visible; VOICEVOX reachable on loopback and not on other interfaces; tokens present; every ignored path actually ignored (`git check-ignore`). Prints the §5b status table. Clear actionable error messages.

**M1 — SRS fetchers (read-only) + text brain loop (no audio).** Built first, in this order, because they need no GPU or audio and the read-only guarantee must be proven before anything else touches the SRS accounts: (a) `backend/srs/http.py` GET-only client — **no setter, no write method, no `post`/`put`/`patch`/`delete`, nothing that could be extended into one without a new ADR**; (b) `srs/wanikani.py` and `srs/bunpro.py` fetchers with disk cache, on that client only; (c) `srs/profile.py` renderer (≤ 600 tokens); (d) `make doctor` extended with the token-permission warning and a live read-only fetch; then (e) the persistent claude subprocess wrapper + CLI REPL: type Japanese, see streamed sentence chunks with timings and per-sentence emotion, **with the real Student Profile already in the prompt**. Tests: read-only recording-transport test (every request in a full fetch of both sources is `GET`; the client has no write attribute) — this test exists from the first commit of `srs/http.py` and runs in every `make test` thereafter; fetcher tests against the V0.5 fixtures; cache TTL and `resync` tests; profile ≤ 600 tokens with zero/one/both sources; chunker unit tests (。！？, ellipses, emotion-tag stripping at turn start **and** sentence start, no mid-sentence splits); env-allowlist test (parent has `ANTHROPIC_API_KEY` and `WANIKANI_TOKEN`, child sees neither); `apiKeySource` assertion test; process restart/`--resume` test with the same `--session-id`; `rate_limit_event` handled without crashing; **verify that MCP tools survive `--tools ""`** (else fall back per §4).

**M2 — Ears & mouth (no avatar).** Mic → VAD → whisper → M1 loop → VOICEVOX → speaker playback in terminal/minimal page. Emotion → voice tone table live (§7). Tests: mora→viseme mapper golden tests against 3 recorded `audio_query` fixtures (commit fixtures); hallucination filter tests; emotion→VOICEVOX params test (each tag produces the configured style/pitch/speed/intonation; unknown style id degrades to base + scalars); VAD gating test (speech during `speaking` needs the higher threshold). Latency and VRAM instrumentation live from here.

**M3 — Face.** Full frontend with TalkingHead, viseme-synced speech, subtitles, emotions (face + voice, set at sentence playback start), listening reactions, idle life, status bar, settings drawer, barge-in end-to-end. Acceptance: 10-turn conversation on headphones where lip-sync looks tight and barge-in cuts speech < 300 ms; **10-turn conversation on laptop speakers with zero self-interruptions**; each of the four emotion tags visibly and audibly distinct in a scripted 4-sentence turn; p90 voice→voice ≤ 5.0 s over 20 turns (3.0 s until 2026-09-10, ADR-033); VRAM ≤ 10 GB steady state.

**M4 — Sensei brain.** Memory and context (§6b, ADR-031/032) — the turn log, the start-of-session read, the end-of-session summariser, and *last* the pre-emptive session rotation, which is the only piece here that can break a working conversation and so ships after several real lessons on the rest. `CONTEXT_ROTATE_AT` unset means never rotate, and that stays a supported configuration. Then: tutor prompt tuning with the real profile, Bunpro MCP (found or written per §5 — read tools only, on the same GET-only client), §5b status indicators wired to real state, `control: resync` from the UI. The fetchers and profile renderer already exist from M1; M4 is about the tutor *using* them well and the user *seeing* that it does. Acceptance: with a real WaniKani token, the tutor demonstrably uses ≥ 3 recent unlocks in a 5-minute conversation (visible in logs); Bunpro absent → clean degradation with the chip reading `disabled`; Bunpro MCP deliberately broken (bad credential) → chip reads `failed`, conversation unaffected; WaniKani offline with a warm cache → chip reads `stale`, profile still present.

**M5 — Polish.** Full settings page (§11: every key, grouped, masked secrets, Test buttons, first-run flow — `.env` is gone, amended 2026-09-12), session summary on goodbye, `--profile` overlay, README with setup for a fresh machine (Linux **and** Windows/WSL2 per §15), docker-compose for VOICEVOX on loopback, `make run`, `make check-secrets`, redaction test green, read-only GET-only test green. Acceptance: a fresh machine goes from clone to first conversation **without any `.env`**, entering tokens only through the settings page, and a launch with none entered says what is missing and what happens meanwhile.

## 13. REPO LAYOUT

```
atama-ai/
├─ backend/
│  ├─ app.py            # FastAPI + WS
│  ├─ claude_session.py # subprocess mgmt, stream-json, resume, env allowlist
│  ├─ chunker.py        # sentence chunking + emotion tags
│  ├─ stt.py  vad.py  tts_voicevox.py  visemes.py
│  ├─ audio.py          # device picker, playback, mic capture (PortAudio)
│  ├─ speaker.py        # speech queue: synthesise N+1 while N plays
│  ├─ chunker.py  prompt.py  repl.py  mcp_ready.py  search_mcp.py
│  ├─ brain/            # Brain interface (ADR-027) + claude_cli provider
│  ├─ emotions.py       # emotion → VOICEVOX style/params table (§7)
│  ├─ status.py         # §5b service status registry → service_status messages
│  ├─ tools/readonly_gate.py  # §0 Golden Rule static gate — runs on every test/run/doctor/commit
│  ├─ srs/http.py       # GET-only client + ReadOnlyTransport — the runtime half of §0
│  ├─ srs/wanikani.py  srs/bunpro.py  srs/profile.py
│  ├─ srs/bunpro_mcp.py # stdio MCP server, read tools only, only if V0.7 chooses "write one"
│  ├─ config.py  constants.py (verified CLI/endpoint findings, dated)
│  ├─ models.py (pydantic WS protocol)
│  ├─ data/             # hallucination_blocklist.txt, fillers.txt
│  └─ tests/            # fixtures/ (sanitised, committed)  fixtures/private/ (ignored)
├─ frontend/            # vite, vanilla TS
│  ├─ index.html  src/{main.ts, avatar.ts, ws.ts, mic.ts, ui.ts, status.ts}
│  └─ public/avatar.glb (git-ignored; README explains export)
├─ prompts/tutor.md
├─ .cache/              # git-ignored, created at startup: mcp.json, tutor_prompt_rendered.txt,
│                       #   claude-cwd/, srs cache, fillers/
├─ logs/                # git-ignored
├─ settings.json        # git-ignored, mode 0600, written by the settings page (§11) — the config store
├─ docker-compose.yml   # voicevox only, published on 127.0.0.1
├─ mcp.json.template  .gitignore  Makefile   (.env exists only for docker compose)
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
- **Mic:** captured by the Windows browser, streamed over the WS. No WSL2 audio device is needed. Playback is also browser-side.
- **VOICEVOX:** Docker Desktop, same `docker-compose.yml`, same loopback binding.
- **Do not** split the backend across the two sides. The repo lives on the WSL2 filesystem (`~/...`, not `/mnt/c/...`) — CUDA model loading and file I/O are substantially slower through the `/mnt/c` bridge, which shows up directly in the latency budget.

`make doctor` reports which topology it detected and checks the WSL2-specific items above when applicable.

**Native Windows (the user's actual setup, discovered 2026-09-09):** Python 3.13 (Microsoft Store build), Node 22, Docker Desktop, RTX 4090 Laptop. M0/M1 run unchanged. Two Windows-specific rules, both verified live: (1) the **Store Python virtualises `%LOCALAPPDATA%`** — directories it creates there are invisible to cmd.exe and to `claude`; the claude cwd therefore defaults to `~/.atama-ai/claude-cwd`, and no path shared with another process may live under AppData; (2) **spawn `claude` without a shell** (`shutil.which("claude")` → `claude.CMD`, executed directly). Whether the Store build can host faster-whisper + CUDA is decided at M2; switching to a python.org/uv-managed CPython is the expected outcome and is not a spec change.
