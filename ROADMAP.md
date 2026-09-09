# atama-AI — Roadmap

How each subsystem gets **built → tested → validated against reality → integrated**, and what
gate it has to pass before the next milestone starts.

- [ATAMA-AI_SPEC.md](ATAMA-AI_SPEC.md) is authoritative. Where this file and the spec disagree,
  the spec wins and this file is wrong — fix it.
- [ADR.md](ADR.md) records *why* the pinned choices are pinned.
- Milestones **M0–M5** are the spec's §12 and are built strictly in order.

## Vocabulary

| Term          | Meaning                                                                                     |
|---------------|---------------------------------------------------------------------------------------------|
| **Test**      | Automated, runs in `make test`, no network, no GPU, no external process. Fast and hermetic.  |
| **Validate**  | Checked against the real external thing (live CLI, live VOICEVOX, real GPU, real ears/eyes). |
| **Integrate** | Wired to its neighbours with the contract asserted at the seam.                              |
| **Gate**      | A binary condition. Not met → the milestone is not done and the next one does not start.     |

Two rules run through everything:

1. **Never fabricate an external interface.** Verify against `claude --help`, VOICEVOX's live
   `/docs` OpenAPI, the TalkingHead README, and the WaniKani docs. Pin the finding in a dated
   code comment and in `backend/constants.py`. This is what **V0** below exists for.
2. **The two hard numbers are gates, not aspirations** — voice→voice p90 ≤ 3.0 s, VRAM ≤ 10 GB.
   They are measured from M2 onward, not discovered at M5.

---

## V0 — Verification spikes (before M1 writes a line of integration code)

Throwaway scripts, deleted or moved into `backend/tests/fixtures/` when done. Each produces a
**pinned finding** committed as a dated comment plus a constant.

| Spike    | Question to answer | Output | Status |
|----------|--------------------|--------|--------|
| **V0.1** | `claude --help`: exact flag spellings for headless stream-json; is there a way to remove *all* built-in tools; how to isolate from user config. | `backend/constants.py` — the exact argv that works, dated. | **Done 2026-09-09** — see findings log below. |
| **V0.2** | Run one real `claude -p` stream-json session. Capture every event type on stdout for a 3-turn conversation **with the Bunpro MCP server configured**, so the per-entry shape of `init.mcp_servers[]` and the `tool_use`/`tool_result` blocks are pinned. | `backend/tests/fixtures/claude_stream_*.jsonl` — replay fixtures for parser and status tests. | **Done 2026-09-09** (single-turn with a real MCP tool call; raw stream in `.cache/claude_probe.jsonl`, to be sanitised into a fixture at M1(e)). Shapes in the findings log. |
| **V0.3** | Hit VOICEVOX `/docs` locally. Confirm `audio_query` response shape, the `pitchScale`/`intonationScale` request fields, and the `GET /speakers` shape used by the emotion table. | 4 real `audio_query` fixtures, a `/speakers` fixture, a pinned schema note. | **Done 2026-09-09** — VOICEVOX 0.25.2, fixtures in `backend/tests/fixtures/voicevox/`. See findings log. |
| **V0.4** | TalkingHead README: `speakAudio` signature; whether `vtimes`/`vdurations` are **ms or s**; the real mood name set; whether gestures exist and their names; the facility for overriding ARKit blendshapes directly (needed for `surprised`/`serious`/`thinking`). | Pinned constants + comment. A wrong timing unit is silent drift; a wrong mood name is a silent no-op. | Open |
| **V0.5** | WaniKani `/v2/user` and `/v2/assignments`: real response shape, pagination, rate-limit headers. | Sanitised fixtures (no personal data beyond what the golden tests need). | **Done 2026-09-09** — 6 endpoints captured live, fixtures in `backend/tests/fixtures/wanikani/`. See findings log. |
| **V0.6** | Baseline VRAM: load faster-whisper `large-v3` @ `int8_float16` alone, read `nvidia-smi`. | A number. If > ~4.5 GB, ADR-004's fallback triggers now, not at M5. | **Done 2026-09-09 — 2 169 MiB.** Comfortably under the 3.5 GB estimate and the 4.5 GB fallback threshold: `large-v3` stays, `medium` is not needed. |
| **V0.7** | **Does a trustworthy Bunpro MCP server exist?** Survey community stdio MCP servers for Bunpro; check they work against the current site/API, what credential they take, whether the code is small enough to read end-to-end (it receives your credentials), and — **disqualifying** — whether it exposes any write tool that cannot be removed from the surface (ADR-021). If none passes, the decision is to write `backend/srs/bunpro_mcp.py` (spec §5) with read tools only. | A decision recorded in ADR (new entry), plus either a pinned version or a stub module. | **Done 2026-09-09** — decision: **write our own** (ADR-023). See findings log. |
| **V0.9** | WaniKani token permissions: confirm from `/v2/user` which fields expose the token's granted permissions, so `make doctor` can warn on a write-capable token (ADR-021). | Pinned field name + a sanitised fixture for both a read-only and a write-capable token. | **Done 2026-09-09 — negative result.** `/v2/user.data` keys are `current_vacation_started_at, id, level, preferences, profile_url, started_at, subscription, username`; **token scopes are not exposed** and probing them would require a write. Read-only scope can only be guaranteed at token creation; the doctor and the settings page *instruct*, they cannot verify. Test pins the absence. |
| **V0.8** | Do MCP tools survive `--tools ""`? Spawn with the Bunpro MCP configured and `--tools ""`; check `init.tools[]` for the MCP tool names. | Pinned: either `--tools ""` stands, or the fallback `--disallowedTools` list of the 20 built-in names from `init.tools`. | **Done 2026-09-09 — `--tools ""` stands**, *provided the MCP server is connected before the first turn*. See findings log ("Claude subprocess, live"). The disallow fallback is retired (tool names vary by platform). |

### V0 findings log

Pinned facts, dated, to be copied into `backend/constants.py` at M0. Re-verify on every CLI or
engine upgrade.

**2026-09-09 — Claude Code 2.1.159 (`claude --help` + one live stream-json run):**

- `--include-partial-messages` spelled exactly so; requires `--print` and
  `--output-format=stream-json`.
- `--input-format stream-json` and `--output-format stream-json` confirmed.
- `--tools ""` — "Use "" to disable all tools" (built-in set). With the spec's original
  `--disallowedTools` list, `init.tools[]` still contained 20 tools: Task, AskUserQuestion,
  CronCreate, CronDelete, CronList, EnterPlanMode, EnterWorktree, ExitPlanMode, ExitWorktree,
  Monitor, NotebookEdit, PushNotification, RemoteTrigger, ScheduleWakeup, Skill, TaskOutput,
  TaskStop, TodoWrite, ToolSearch, Workflow. That list is the V0.8 fallback.
- `--disallowedTools` and `--disallowed-tools` both accepted; `--allowedTools` likewise.
- `--strict-mcp-config` — "Only use MCP servers from --mcp-config, ignoring all other MCP
  configurations".
- `--session-id <uuid>` — orchestrator-assigned id; `--resume <id>` after crash.
- `--fallback-model <model>` — automatic fallback when the default model is overloaded.
- `--bare` — "OAuth and keychain are never read"; also skips CLAUDE.md auto-discovery, which
  confirms non-bare mode *does* auto-discover CLAUDE.md from cwd. Never use `--bare`; always spawn
  from an empty cwd.
- `--no-session-persistence` exists — never pass it.
- `setup-token` subcommand — "Set up a long-lived authentication token (requires Claude
  subscription)". Source of `CLAUDE_CODE_OAUTH_TOKEN`.
- `init` event keys: `agents, analytics_disabled, apiKeySource, claude_code_version, cwd,
  fast_mode_state, mcp_servers, memory_paths, model, output_style, permissionMode, plugins,
  product_feedback_disabled, session_id, skills, slash_commands, subtype, tools, type, uuid`.
  Under subscription auth `apiKeySource == "none"` — the `make doctor` auth assertion.
- `result` event keys: `api_error_status, duration_api_ms, duration_ms, fast_mode_state,
  is_error, modelUsage, num_turns, permission_denials, result, session_id, stop_reason, subtype,
  terminal_reason, total_cost_usd, ttft_ms, type, usage, uuid`. `ttft_ms` and `duration_api_ms`
  are the Claude-stage ground truth for the timing log.
- A `rate_limit_event` event type is emitted (seen even on a trivial call). Payload shape still
  to be captured in V0.2.

**2026-09-09 — Bunpro API and MCP landscape (V0.7, web survey; to be confirmed live at M1):**

- **No official API.** Bunpro staff (Sean, 2024-09-13): "We previously had an API, with Docs and
  everything … we deprecated the API and removed the Docs." Reverse-engineering the site's
  frontend API is explicitly permitted (Sean, 2025-12-18: "Yeah you can, that's fine. Just note
  that they may change without warning."). Through 2026 staff have been handling API access
  requests by DM only; nothing public. **Treat every endpoint as unstable.**
- **Base:** `/api/frontend/*` — seen as both `https://bunpro.jp/api/frontend` and
  `https://api.bunpro.jp/api/frontend`; pin whichever answers at M1.
- **Two credential schemes** against the same endpoints:
  1. **Settings → API "Account API Token"**, sent as `Authorization: Token token=<token>`.
     Used by the two newest projects (yash-278/bunpro-mcp, updated 2026-09-06;
     sawariz0r/bunpro-kanji, 2026-06). Staff said in 2024 that this key "does nothing" — it
     evidently works now. **This is our scheme**: it needs no password and no browser.
  2. Browser cookie `frontend_api_token` (a JWT, ~2-day to ~monthly lifetime), sent as
     `Authorization: Bearer <jwt>`. Used by the community OpenAPI spec
     (cbullard-dev/bunpro-community-api), PatVandyke/bunpro-mcp, and jbeshir/mcp-servers (which
     obtains it by POSTing the user's **email + password** to `/users/sign_in`). Not ours.
- **Read endpoints we need** (from the community spec, all `GET`): `/user` (profile),
  `/user/due` (reviews due), `/user/queue`, `/user_stats/jlpt_progress_mixed`,
  `/user_stats/srs_level_details?reviewable_type=Grammar&level=…`,
  `/user_stats/srs_ghost_level_details?reviewable_type=Grammar` (ghosts),
  `/user_stats/forecast_daily`. Write endpoints exist on the same API (`/reviews/add_to_reviews`,
  `/reviews/{id}/update`, `/settings/update`, bookmarks, known-kanji…) — **never called**; their
  paths are listed here only so the gate's fixture tree can include them as violations.
- **Existing MCP servers surveyed:**
  | project | tools | read-only? | credential | verdict |
  |---|---|---|---|---|
  | PatVandyke/bunpro-mcp (TS) | 19 read + `add_to_reviews`, `remove_from_reviews`, `add_bookmark`, `remove_bookmark` | **no** | cookie JWT | **disqualified** — write tools (Golden Rule) |
  | jbeshir/mcp-servers/bunpro (Go, MIT) | 12, all read | yes | **email + password** → Devise login → cookie | **disqualified** — we will not hold a password |
  | yash-278/bunpro-mcp (TS, MIT) | 8, all read; README: "cannot answer reviews, start lessons or crams, change SRS progress, edit decks, or update account settings" | yes | Settings→API token, `Token token=` | acceptable in principle; not chosen — tools are stats/heatmap-shaped, none returns ghost or per-grammar-point SRS detail, and it is built for hosted HTTP deployment (Railway, OAuth) with ~14 KB of HTTP-server code we would carry for nothing |
- **Decision (ADR-023): write `backend/srs/bunpro_mcp.py`** — three read tools on our GET-only
  client; Settings→API token only; ≤ 1 request/s politeness (yash-278 uses a 2 s minimum
  interval, sawariz0r ≤ 1 req/s — 2 s × 5 session-start calls overshoots the 10 s budget, so 1 s); every response validated against a pinned fixture
  so an upstream change fails loudly into the `bunpro_mcp` chip rather than silently.
- **Security audit of the surveyed servers (where does the token go?):**
  | project | outbound hosts | base URL overridable? | deps | telemetry / logging of secrets |
  |---|---|---|---|---|
  | yash-278/bunpro-mcp | `https://api.bunpro.jp` only, **hardcoded constant**, route allowlist ("only permits its fixed read-only Frontend API routes") | **no** | `@modelcontextprotocol/server`, `zod` only; no install scripts | none; stdio mode logs one startup line to stderr. **Hosted mode** is the one place a token leaves your machine: it is passed through *the operator's* Railway server as `X-Bunpro-Token` — never use someone else's hosted URL |
  | jbeshir/mcp-servers/bunpro | bunpro.jp login + API; login URL is a **parameter / optional `BUNPRO_LOGIN_URL` env** — a wrong value would ship the **password** elsewhere | yes (env) | Go stdlib | no logging of credentials seen |
  | PatVandyke/bunpro-mcp | not audited further — disqualified for write tools | — | `@modelcontextprotocol/sdk`, `zod`; `prepare` runs `tsc` | — |
  Conclusion: nothing dodgy in local/stdio use; the two lessons adopted into §0 are **hardcode the
  origins** (no overridable base URL) and **enforce a host allowlist at the transport**, so a
  token cannot be sent anywhere but WaniKani/Bunpro even by a bug.
- **WaniKani MCP:** not needed (spec §5 uses the official REST API directly). For the record,
  jbeshir/mcp-servers/wanikani is read-only (6 tools, token via env); jackedney/wanikani-mcp
  has `register_user`/`sync_data` writes and needs PostgreSQL — irrelevant to us.

**2026-09-09 — live SRS captures (M1, both tokens on the user's machine):**

- **Bunpro auth (corrects the survey):** `Authorization: Token token=<Settings→API token>` alone →
  401 `{"errors":[{"code":"AUTH_USER_DENIED"}]}` on every endpoint. It works only with the query
  parameter **`dangerously_authenticate_using_api_token=true`** and `Origin: https://bunpro.jp` /
  `Referer: https://bunpro.jp/` (from yash-278's source). Origin `https://api.bunpro.jp`
  confirmed. Pinned in `constants.py`; the client adds the parameter to every Bunpro request.
- **Bunpro shapes** (fixtures in `backend/tests/fixtures/bunpro/`): `/user` → `user.data.attributes.{username, level (XP level, not JLPT), …}`;
  `/user/due` → `{total_due_grammar, total_due_vocab}`; `/user_stats/jlpt_progress_mixed` →
  `{grammar: {"5".."1": {beginner, adept, seasoned, expert, master, total_count}}, vocab: {…}}`;
  `/user_stats/srs_level_overview` → `{grammar: {…, ghost, self_study}, vocab: {…}}`;
  `/user_stats/srs_ghost_level_details?reviewable_type=Grammar` → `{type, reviews: {data: [ghost_review{attributes: {reviewable_id, streak, next_review, is_slain}}], included: [reviewable{attributes: {title, meaning, level: "JLPT4", slug}}]}}`;
  `/user_stats/srs_level_details?reviewable_type=Grammar&level=<name>` → same layout + `pagy`;
  **`level` must be one of `beginner|adept|seasoned|expert|master`** — numeric or missing → HTTP 500;
  `/user_stats/forecast_daily` → `{grammar: {later, tomorrow, "YYYY-MM-DD": n…}, vocab: {…}}`;
  `/user/queue` → JSON:API `deck_setting` rows with `included` decks (slug, title, grammar_count).
- **WaniKani shapes** (fixtures in `backend/tests/fixtures/wanikani/`): `/v2/user.data.{level, username, subscription.{active,type,max_level_granted}}` (no permissions field — V0.9);
  `/v2/assignments?subject_types=vocabulary&started=true` → 219 items in one page (`per_page` 500, `next_url` null), item `data.{subject_id, srs_stage 1–9, unlocked_at, started_at, passed_at, burned_at, available_at, hidden}`;
  `/v2/review_statistics?…&percentages_less_than=80` → 0 items for this account (leech path tested with a synthetic row);
  `/v2/summary.data.{reviews[{available_at, subject_ids}], lessons, next_reviews_at}`;
  `/v2/subjects?ids=…` → `data[].{id, object: "vocabulary", data.{characters, level, slug, meanings[{meaning, primary}], readings[{reading, primary}], parts_of_speech, …mnemonics}}`.
- **Politeness vs budget:** 2 s Bunpro spacing × 7 calls = 12 s > the 10 s session-start budget
  (found by the timeout path in tests). Set 1 s; trimmed to 6 Bunpro calls; measured launch fetch
  of both sources **4.6 s**. Rendered profile with real data: **593 tokens** (cap 600).
- **Fetch policy (ADR-024):** launch + manual refresh only; snapshots with `fetched_at`; MCP
  server reads the snapshot, holds no token; `mcp.json` carries no credential (verified: 0
  occurrences of "TOKEN").
- **Our MCP server, direct stdio handshake:** initialise 0.68 s, tools
  `[get_review_queue, get_ghost_reviews, get_grammar_progress]`, `get_ghost_reviews` returns
  the real ghost (そういう, N4, streak 1) with `synced_at`/`age_minutes`.
- **mcp SDK:** installed `mcp` **2.2.0** — `FastMCP` is gone; `from mcp.server.mcpserver import
  MCPServer`, `@server.tool(name=…)`, `server.run("stdio")`, `await server.call_tool(name, args)`.
- **Platform:** the user's machine is **native Windows 11** (Python 3.13 Store build, Node 22,
  Docker 29, RTX 4090 Laptop 16 GB), not WSL2. Everything so far is platform-neutral; §15's WSL2
  assumption is revisited at M2 (CUDA for faster-whisper on native Windows).
- **Microsoft Store Python virtualises `%LOCALAPPDATA%` (MSIX).** A directory the venv's
  Python created at `%LOCALAPPDATA%\atama-ai\` physically landed in
  `%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.3.13_…\LocalCache\Local\atama-ai\`;
  Python saw it, cmd.exe/`claude.CMD`/`icacls` did not ("The current directory is invalid.").
  Consequences: the claude cwd default is `~/.atama-ai/claude-cwd` (profile root is not
  virtualised); **nothing shared with another process may live under AppData when running the
  Store build**; the repo (`C:\CodeProjects\…`) and `%TEMP%` are unaffected. Recommendation for
  M2: install python.org / uv-managed CPython instead of the Store build (CUDA/torch wheels and
  this virtualisation are both easier without it) — decision deferred to M2, not blocking now.
- **Spawn without a shell.** `subprocess` with `shell=True` is unnecessary (resolve
  `shutil.which("claude")` → `claude.CMD`, exec directly) and is an injection surface.

**2026-09-09 — Claude subprocess, live (V0.2 / V0.8), through the full spawn hygiene
(allowlisted env, `--strict-mcp-config`, our credential-free `mcp.json`, `--session-id`):**

- **`--tools ""` + MCP works** — `init.tools == ['mcp__bunpro__get_ghost_reviews',
  'mcp__bunpro__get_grammar_progress', 'mcp__bunpro__get_review_queue']`,
  `init.mcp_servers == [{"name": "bunpro", "status": "connected"}]`, `apiKeySource: none`, the
  tool was called (`tool_use` → `tool_result` with the real ghost), reply `そういう が幽霊レビュー待ち。`,
  `ttft_ms: 1490`, `num_turns: 2`.
- **…but only if the server is connected before the first turn.** Three earlier runs wrote
  the user turn immediately: `init.mcp_servers` said `pending`, `init.tools` was `[]`, and the
  model hallucinated a tool call / said it had no such tool. Claude Code emits **no** MCP
  connection event on stdout (grep of the raw streams: only `system/init`, `system/status`
  `{"status":"requesting"}`, `rate_limit_event`, `stream_event`, `assistant`, `user`, `result`).
  Fix: the server registers a handler for `notifications/initialized` (mcp 2.2.0
  `Server.add_notification_handler`, documented to run "after the runner has marked the connection
  initialized") and writes `ATAMA_MCP_READY`; observed **1.11 s** after spawn. The orchestrator
  waits on the marker; the probe does the same. No timers.
- **The `--disallowedTools` fallback is retired.** On this Windows box the built-in set was
  `Bash, Edit, Glob, Grep, PowerShell, Read, TaskCreate, TaskGet, TaskList, TaskUpdate, WebFetch,
  WebSearch, Write` — different from the 20 names the first probe saw. No static list is
  reliable; `--tools ""` is.
- **cwd must be outside the repo.** From `.cache/claude-cwd/` (inside the repo)
  `init.memory_paths.auto` was `~/.claude/projects/C--CodeProjects-Atama-AI/memory/` — the
  subprocess was attached to the Atama-AI project (CLAUDE.md discovered up the tree; the model
  mentioned "the atama-AI backend" unprompted). Default moved to `%LOCALAPPDATA%\atama-ai\claude-cwd`
  / `$XDG_STATE_HOME/atama-ai/claude-cwd` (`config.claude_cwd()`).
- **`rate_limit_event` payload:** `{"rate_limit_info": {"status": "allowed", "resetsAt": <epoch>,
  "rateLimitType": "five_hour", "overageStatus": "rejected", "overageDisabledReason":
  "org_level_disabled", "isUsingOverage": false}, "uuid", "session_id"}` — emitted once per
  session even when allowed; `status != "allowed"` is the chip trigger.
- **Permission behaviour in `-p`:** a non-allowed built-in (`Bash`) returned
  `tool_result is_error=true "This command requires approval"` — denied, never executed. Keep
  `--allowedTools` on the three MCP names regardless (ADR-021 belt-and-braces).
- **Stream shape:** `stream_event` wraps Anthropic SSE (`message_start`, `content_block_start`,
  `content_block_delta` ×N, `content_block_stop`, `message_delta`, `message_stop`); `assistant`
  events carry complete content blocks (`text` / `tool_use{name,input}`); `user` events carry
  `tool_result{content,is_error}`; `system/status {"status":"requesting"}` precedes each API call.

**2026-09-09 — M1(e), the brain running for real (spec §4, ADR-027/029):**

Four bugs, each of which presented as something else. Written down because the symptom never
pointed at the cause:

- **`init` arrives only AFTER the first user turn.** `start()` waited for `init` before sending
  anything; the CLI waits for a turn before emitting `init`. Mutual deadlock, reported as "claude
  did not emit an init event". The `apiKeySource` assertion therefore happens on the first turn,
  and `make doctor` is the pre-flight that catches bad auth before the app runs. The MCP ready
  marker, by contrast, *does* appear without a turn (~1.1 s), so `start()` waits on that.
- **stderr must be drained continuously.** With `--verbose` the CLI fills the stderr pipe buffer,
  then blocks on write and stops producing stdout. Looked exactly like a slow model. A second
  pump thread keeps a bounded tail, which also gives startup failures something to report.
- **Multi-line `--system-prompt` truncates at the first newline AND swallows the following
  flags.** Our prompt has lines starting with `-`; `--mcp-config` placed after it was silently
  ignored and the tutor ran with **no tools**, with nothing logged. Fixed by
  `--system-prompt-file` (ADR-029). The one-line version of the same prompt parsing correctly is
  what identified newlines as the trigger.
- **`--append-system-prompt` leaves the coding agent in charge.** Sensei introduced herself as
  「私はClaude Codeです…ソフトウェアエンジニアリングのタスクを支援します」 in markdown bullets, and
  `init.tools` was empty. `--system-prompt-file` (replace) → in character, tools present.

Also found: **Claude Code exports `CLAUDE_EFFORT` and friends into the shell**, and our config
keys have the same names, so running the app from inside a Claude Code session picked up the
parent's values. Process-environment overrides now require the `ATAMA_` prefix; the `.env` file
keeps bare names.

**Measured (sonnet, `--effort high`, prompt 1 739 tokens: soul 345 + profile 594):** first
sentence 1.8–2.4 s, ttft 1.6–2.2 s, MCP tool call 10 ms. The Claude stage budget is 1.60 s
(§10) — currently over, with the levers (`--effort`, model, prompt size) untouched. Latency is
M3's gate, not M1's; the numbers are recorded here so the M3 work starts from data.

**2026-09-09 — VOICEVOX 0.25.2 (V0.3), live:**

- `audio_query` keys: `accent_phrases, prePhonemeLength, postPhonemeLength, speedScale, pitchScale,
  intonationScale, pauseLength, pauseLengthScale, volumeScale, outputSamplingRate, outputStereo, kana`.
  `accent_phrase` = `{accent, is_interrogative, moras, pause_mora}`;
  `mora` = `{text, consonant|null, consonant_length|null, vowel, vowel_length, pitch}`.
- Phoneme alphabet: `a/i/u/e/o` voiced, **`A/I/U/E/O` devoiced** (uppercase), `N` for ん, `cl` for っ,
  `pau` on `pause_mora`. Note `N` is uppercase but is a phoneme, not a devoiced vowel — a mapper that
  simply lower-cases would turn ん into a vowel.
- Defaults: `prePhonemeLength` 0.1 s, `postPhonemeLength` 0.1 s, `speedScale` 1.0, 24 kHz mono WAV.
- **`prePhonemeLength` IS divided by `speedScale`.** Confirmed by computing the timeline and comparing
  with the real synthesised WAV across 4 samples x 2 speeds: agreement within 46 ms worst case, most
  under 30 ms (VOICEVOX rounds to sample boundaries, so exact equality would be asserting its rounding).
- `GET /speakers` -> `[{name, speaker_uuid, styles: [{id, name, type}], supported_features, version}]`,
  43 speakers. **Styles are resolved by NAME**, not id, so an engine upgrade renumbering ids is safe.
  Chosen default: **No.7** — ノーマル=29, アナウンス=30 (crisp/formal), 読み聞かせ=31 (warm read-aloud):
  a good spread for a teacher.
- Synthesis of one short sentence takes **470-740 ms** on CPU. The §10 budget for "VOICEVOX first chunk
  + WS delivery" is 400 ms, so this is over already, before any WebSocket. Levers for M3: shorter first
  sentences, and starting synthesis on the first sentence while the rest still streams (already done).

**2026-09-09 — the ears (M2), on the target box:**

- **CUDA works under the Microsoft Store Python** for CTranslate2 — but the runtime is not bundled.
  The model *loads* on the GPU and then inference dies with `Library cublas64_12.dll is not found`.
  `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` supplies the DLLs, and on Windows their
  directories must be registered with `os.add_dll_directory` before the first inference
  (`stt.enable_cuda_libraries`). No system-wide CUDA toolkit needed.
- **`large-v3` @ `int8_float16` = 2 169 MiB**, load 5 s (warm disk), warm-up transcribe 2.1 s.
  Whole-pipeline VRAM stays far inside the 10 GB cap (§10b).
- **The warm-up on one second of pure zeros returned 「ご視聴ありがとうございました」** — the exact
  hallucination spec §9 predicted, reproduced on the first run. The blocklist is a data file and
  the filter requires BOTH a blocklist match AND quiet audio, because a student really can say
  ありがとうございました.
- **Silero VAD v5 ONNX** (2.3 MB, CPU, no torch): inputs `input (batch, samples)`,
  `state (2, batch, 128)`, `sr () int64`; outputs speech probability and the next state. Frames are
  **exactly 512 samples at 16 kHz** — the model is stateful and a different frame size degrades its
  judgement rather than erroring. Silence scored 0.0006, light noise 0.0016.
- **The barge-in factor could not be a multiplier** (ADR-018 addendum): 0.5 x 2.0 = 1.0 is
  unreachable for a probability, which would have disabled barge-in with no error anywhere.

**Gate V0:** every external interface this project touches has a pinned, dated finding in the
repo. No code calls an unverified interface.

---

## Subsystem plan

Each subsystem below is owned by one module and has one contract. Build them in the milestone
order given; the integration column is what makes the milestone demoable.

**Build order inside M1:** subsystems 12 → 13 (fetcher) → 14 → 1 → 2. The SRS fetchers come
first because they need no GPU or audio, the read-only guarantee (ADR-021) must be proven before
anything else touches the accounts, and the text REPL is a far better test of the tutor with a
real profile in the prompt.

### 0. Read-only gate (Golden Rule, spec §0) — `backend/tools/readonly_gate.py` + `backend/srs/http.py` — **M0, before anything else**

**Contract:** it is impossible to build, start, test, or commit this repo with code that could
set or write through the WaniKani or Bunpro API keys. The gate exists before the first SRS
module does.

- **Test** — The gate is tested against a fixture tree of deliberate violations, one per rule,
  and must catch every one: an `httpx.post` in `srs/`; a `.request(method="PUT")`; a
  `method=` variable; a function named `set_level`, `update_cache`, `create_note`,
  `mark_reviewed`, `start_assignment` in `srs/` (each rejected **by name**, even when the body
  is harmless); an `import requests` outside `srs/http.py`; a second public method on the
  client; a fourth MCP tool; a write-scope string outside the doctor; an `https://` literal in
  `srs/` that is not one of the two pinned origins; a `base_url=` parameter or env read on the
  client. Also a clean fixture tree that passes. Runtime host test: a `GET` to
  `https://example.com` through the client raises `HostViolation` and nothing is sent; a 302
  from an allowed host to any other host is not followed. The runtime `ReadOnlyTransport` test issues a `POST` through the client's
  underlying transport and asserts `ReadOnlyViolation`, that nothing was sent, and that the
  status registry received `error`. The import-time self-check test monkeypatches a second
  method onto the client class and asserts import fails. Frontend: a `wanikani.com` string in a
  temp `frontend/src` file makes `npm run build` fail.
- **Validate** — `make test`, `make run`, `make doctor` each visibly run the gate first (it
  prints one line: rules checked, files scanned, `OK`). `make hooks` installs the pre-commit
  hook; a commit containing a violation is rejected.
- **Integrate** — Makefile target ordering; `srs/http.py` is the only HTTP import in `srs/`.
- **Gate M0a** — All violation fixtures caught, clean tree passes, hook installed and
  demonstrated. **No SRS fetcher is written before this gate is green.**

### 1. Claude session — `backend/claude_session.py` — **M1**

**Contract:** `send(user_text) -> async iterator of text deltas + lifecycle events`. One
persistent subprocess, isolated per ADR-016. Survives a crash without losing conversation memory.

- **Test** — Feed the V0.2 stream-JSON fixtures through the parser: `init` yields `session_id`
  equal to the one we passed, plus `apiKeySource`, `tools`, `mcp_servers`; partial deltas
  accumulate to the same text as the complete assistant message; `tool_use`/`tool_result` pairs
  are emitted as status events; `rate_limit_event` is surfaced, not fatal; `result` closes the
  turn with `ttft_ms`/`duration_api_ms` recorded; an unknown event type is logged and skipped.
  **Env allowlist test:** parent has `ANTHROPIC_API_KEY`, `WANIKANI_TOKEN`, `BUNPRO_API_TOKEN`
  set — the spawned child's env contains none of them and does contain `PATH`. **Auth test:**
  `apiKeySource != "none"` → the session refuses to start and raises a clear error. **cwd test:**
  the child is spawned from `.cache/claude-cwd/`, which contains no `CLAUDE.md`, and the same
  cwd is used on restart. Timeout test: a turn that never produces `result` gets SIGINT at the
  configured deadline and surfaces the apology line.
- **Validate** — Live: 10-turn conversation. Kill `-9` the child mid-turn; assert it restarts
  with `--resume <our session id>` and still remembers turn 1. Inspect `init.tools[]` — it must
  be empty or MCP-only (V0.8). Confirm `apiKeySource == "none"` in the real init.
- **Integrate** — With the chunker (subsystem 2) behind the M1 CLI REPL.
- **Gate M1a** — Restart/resume passes; no secret can reach the child; `apiKeySource` is
  asserted; a wedged turn cannot hang the app; built-in tools are gone.

### 2. Sentence chunker — `backend/chunker.py` — **M1**

**Contract:** stream of text deltas → stream of `(sentence, emotion | None)`. Pure function over
a stream. No I/O.

- **Test** — Splits on `。！？` and newline; does **not** split mid-sentence when a delta arrives
  split across a boundary character; handles `…` and `‥` without emitting an empty chunk;
  strips exactly one leading `[happy]|[thinking]|[surprised]|[serious]` **at turn start and at
  any sentence start**, attaching it to that sentence; a sentence without a tag inherits the
  previous sentence's emotion within the turn; brackets appearing mid-sentence are left alone
  (and logged — the prompt forbids them); flushes a trailing partial sentence when the turn
  ends. Property test: concatenating all emitted chunks reproduces the input minus the tags.
- **Validate** — Run over the V0.2 real transcripts; eyeball that no chunk would sound wrong
  spoken alone, and that tags land where the prompt says they should.
- **Integrate** — Sits between subsystem 1 and subsystems 5/17.
- **Gate M1b** — The M1 REPL prints Japanese sentence chunks with per-chunk timings and emotion,
  first chunk visibly well before the reply finishes.

### 3. VAD — `backend/vad.py` — **M2**

**Contract:** frame stream (16 kHz mono PCM16) + current state → `speech_start` / `speech_end`
events.

- **Test** — Synthetic frames: silence → speech ≥ 300 ms → silence 600 ms fires exactly one
  `speech_end`; a 200 ms blip fires none; a 400 ms gap mid-utterance does not split the turn.
  **State gating (ADR-018):** in `speaking`, speech at the listening threshold does *not* fire;
  speech at ≥ `BARGEIN_THRESHOLD_FACTOR`× sustained ≥ 250 ms does; anything in the first 150 ms
  of playback is ignored. Pause detection emits the ≥ 300 ms pause events the avatar's nod
  reactions use.
- **Validate** — Live mic in a real room. Tune the silence window against actual conversational
  pauses; confirm the 0.50 s VAD budget line holds.
- **Integrate** — Upstream of STT; also the barge-in trigger (subsystem 8) and the listening
  reactions (subsystem 9).have you planned the conne
- **Gate M2a** — No false end-of-turn in 3 minutes of natural speech with normal pauses.

### 4. STT — `backend/stt.py` — **M2**

**Contract:** utterance PCM → Japanese transcript, or `None` if it should be discarded.

- **Test** — Hallucination filter over a fixture list: ご視聴ありがとうございました and the
  おやすみなさい variants on near-silent input are discarded; the same strings on genuinely loud
  input are kept. Threshold tests on avg-logprob and no-speech-prob. The blocklist is loaded
  from `backend/data/hallucination_blocklist.txt`, and a test asserts adding an entry needs no
  code change.
- **Validate** — Live: 20 recorded utterances at the user's own speaking level; measure warm STT
  wall time against the 0.35 s budget. Confirm the startup 1 s dummy warm-up actually removes the
  first-turn penalty (measure with and without). Report the model's VRAM to the `stt` status
  chip.
- **Integrate** — VAD → STT → Claude session.
- **Gate M2b** — Warm STT p90 ≤ 0.35 s on the target GPU; zero hallucinated transcripts across a
  2-minute silent recording.

### 5. TTS client — `backend/tts_voicevox.py` + `backend/emotions.py` — **M2**

**Contract:** `(sentence, emotion) → (wav_bytes, audio_query_json)`.

- **Test** — Against a stubbed HTTP layer: `audio_query` then `synthesis` are called in order
  with the **emotion's** style id; the emotion table's `speedScale`/`pitchScale`/
  `intonationScale` reach the query; neutral uses the base style and `.env` defaults; a mapped
  style id missing from the `/speakers` fixture degrades to base style + scalars with a warning;
  a VOICEVOX 5xx or timeout degrades to a logged error, a skipped chunk, and a `voicevox=down`
  status — never a dead turn.
- **Validate** — Live engine: measure first-chunk synthesis + WS delivery against the 0.40 s
  budget for a typical ≤ 25-character sentence. Confirm the engine is on CPU (`nvidia-smi` shows
  no VOICEVOX process). Listen to the same sentence under all five emotions; tune the table by
  ear until each is distinct without being cartoonish.
- **Integrate** — Chunker → TTS, one sentence at a time, **overlapping** the next sentence's
  generation.
- **Gate M2c** — First-chunk audio ≤ 0.40 s p90; sentence *N* synthesises while sentence *N+1*
  is still being generated (visible in the timing log); five emotions audibly distinct.

### 6. Viseme mapper — `backend/visemes.py` — **M2**

**Contract:** `audio_query` JSON → `(visemes[], vtimes[], vdurations[])`. One pure function.

- **Test** — Golden tests against the three committed V0.3 fixtures. Table test for the full
  mora→viseme map (spec §7). Timing tests: `prePhonemeLength` offsets the first viseme;
  durations are divided by `speedScale` — including the per-emotion `speedScale`, so the
  timeline stays aligned when the voice speeds up; `pause_mora` and っ produce `sil`; w/y are
  skipped so the vowel dominates; devoiced (uppercase) vowels keep their shape and are flagged
  for the frontend's ~0.4 weight cap. Invariant test: the last viseme end time never exceeds
  the WAV duration.
- **Validate** — Eyes on the avatar (needs M3). Say a `ぱぴぷぺぽ` line and a `さしすせそ` line and
  watch for correct closure and drift at the end of long sentences, under `surprised` (fastest)
  and `serious` (slowest).
- **Integrate** — TTS → mapper → WS `speak` message.
- **Gate M2d** — Golden tests green; timeline end time within one frame of the WAV duration at
  every emotion speed.

### 7. WebSocket protocol — `backend/models.py` + `frontend/src/ws.ts` — **M2 → M3**

**Contract:** the message set in spec §8, defined once and mirrored in pydantic.

- **Test** — Every server→client message round-trips through its pydantic model, including
  `service_status`, `settings` (echo) and `timing`; an unknown message type from the client is
  rejected with an `error`, not an exception. `settings` partial updates apply only the provided
  keys and echo the full applied set; a `model` change triggers the respawn path and a
  `claude=restarting` status. A contract test asserts the TS constants and the pydantic model
  names are the same set — a mismatch fails CI rather than showing up as a silent no-op in the
  browser. `stt_partial` exists in both sets and is never emitted.
- **Validate** — Reconnect behaviour: kill the server mid-session, confirm the client
  auto-reconnects, the status bar shows the reconnect, and the session recovers.
- **Integrate** — The seam between backend and frontend; frozen at M2 so M3 is pure frontend.
- **Gate M3a** — Protocol frozen and mirrored; drift fails CI.

### 8. Barge-in — spans `vad.py`, the WS layer, and `frontend/src/mic.ts` — **M3**

**Contract:** genuine user speech while `speaking` → audio stops, server flushes the TTS queue,
remaining assistant text is marked undelivered, the new speech becomes the next turn. The
avatar's own voice through the speakers must **not** trigger it (ADR-018).

- **Test** — State-machine tests: `speaking` + `bargein` → `listening`; the flushed queue emits
  nothing afterwards; undelivered text is recorded in the turn log. Client-side: `getUserMedia`
  is requested with `echoCancellation: true`, `noiseSuppression: true`, `autoGainControl: false`
  (assert the constraints object).
- **Validate** — Live, two runs: on headphones, interrupt the avatar 10 times and measure
  client-side stop latency; on laptop speakers at normal volume, hold a 10-turn conversation
  **without** interrupting and count self-interruptions.
- **Integrate** — Client-side detection with server confirmation (`bargein_ack` → `bargein`),
  state-gated thresholds from subsystem 3.
- **Gate M3b** — Headphones: speech stops in **< 300 ms**, 10/10, no stale audio after.
  Speakers: **zero** self-interruptions in 10 turns.

### 9. Avatar & frontend — `frontend/src/{avatar,ui,main,status}.ts` — **M3**

**Contract:** `speak` message → lip-synced playback with the sentence's emotion applied at
playback start; `state` → mic indicator and listening reactions; `service_status` → status bar;
`settings` drawer ↔ server.

- **Test** — Viseme queueing logic, the emotion→mood/blendshape table, and the status-chip state
  machine as plain unit tests, headless. The emotion is applied by the audio-start callback, not
  on message receipt (test with a delayed start).
- **Validate** — By eye: 10-turn conversation, lip-sync tight, blinks and sway present but not
  distracting, subtitles toggle JP / off, waist-up framing. Listening reactions: attentive pose
  when the user starts speaking, a nod on pauses, "considering" idle while `thinking`. Status
  bar: pull the WaniKani token → chip goes `disabled`; kill VOICEVOX → `down` within one turn.
- **Integrate** — `speakAudio` **only** — never `speakText` (ADR-007).
- **Gate M3c** — The M3 acceptance conversation: 10 turns, tight lip-sync, barge-in passing,
  status bar truthful.

### 10. Latency instrumentation — cross-cutting — **M2 onward, enforced at M3**

**Contract:** every turn logs a structured stage breakdown; rolling p50/p90 tracked per session;
the same record is sent to the client as `timing`.

- **Test** — The timing record is emitted for every turn including barged-in and errored ones;
  the Claude stage uses `result.ttft_ms` / `duration_api_ms` as ground truth; filler-masked
  turns log **true first-content latency separately** from perceived latency.
- **Validate** — A scripted 20-turn conversation produces the p50/p90 report.
- **Integrate** — `--profile` overlay and the session JSONL.
- **Gate M3d (hard)** — **voice→voice p90 ≤ 3.0 s over 20 turns.** Fillers do not count toward
  meeting it. Missing this gate blocks M4.

### 11. VRAM instrumentation — cross-cutting — **M2 onward**

- **Validate** — `make doctor` and the `--profile` overlay read `nvidia-smi`; a warning fires
  above 10 GB; the `stt` chip shows the model's share.
- **Gate M3e (hard)** — Steady-state total ≤ 10 GB with the browser open and a conversation
  running. Over cap → fall back to Whisper `medium` int8 per ADR-004 and re-measure.

### 12. WaniKani fetcher — `backend/srs/http.py` (M0) + `backend/srs/wanikani.py` — **M1**

Built **before** the Claude session so the read-only guarantee is proven first and the M1 REPL
runs with a real profile. `srs/http.py` is the GET-only client every SRS module is built on: it
has no setter and no write method, and a test asserts that. Adding one requires a new ADR
superseding ADR-021 — which the user has said will not happen.

- **Test** — Against the V0.5 fixtures: level, per-stage counts, ~30 recent unlocks (kanji +
  reading + meaning), ~15 leeches derived by low stage + high incorrect count. Disk cache under
  `.cache/` honours the 1 h TTL; a stale cache is refreshed, a fresh one is not re-fetched;
  `control: resync` bypasses the TTL but not the rate limiter. Rate limiting stays under
  ~60 req/min. Status transitions: no token → `disabled`; fetch ok → `ok`; fetch fails with
  cache → `stale`; fetch fails without cache → `error`. The token never appears in a status
  `detail` or `last_error`. **Read-only (ADR-021):** the fetcher is built on
  `backend/srs/http.py`, which has a `get()` method and nothing else — a test asserts no
  `post`/`put`/`patch`/`delete` attribute exists; a recording transport asserts every request in
  a full fetch is `GET`; the `/v2/user` permissions fixture with a write-capable token makes
  `make doctor` warn.
- **Validate** — One live fetch with a real token, inside the 10 s session-start budget. Confirm
  the real token was created with no write scopes (doctor prints the granted permissions).
- **Gate M1c** — Absent token, expired token, and network failure each produce a clean session
  with the correct status (chip from M3; console/log status from M1). Read-only recording test
  green.

### 13. Bunpro fetcher (M1) + MCP (M4) — `backend/srs/bunpro.py`, `backend/srs/bunpro_mcp.py` (if written), `mcp.json`

Treated as fragile by design (ADR-010). The fetcher lands in **M1** on the GET-only client,
alongside WaniKani. The MCP server is whatever V0.7 decided and lands in **M4**.

- **Test** — Every call path wrapped: malformed response, auth failure, timeout, and total
  absence each log a warning and continue. `mcp.json` is generated from the template and `.env`
  into `.cache/`, and a test asserts no credential is ever written to a tracked path and that
  the credential appears **only** in the Bunpro entry's `env` block. If we wrote the MCP server:
  its three tools return the documented shapes against fixtures and never echo the credential in
  an error. **Read-only (ADR-021):** the MCP server's tool list is exactly
  `get_review_queue`, `get_ghost_reviews`, `get_grammar_progress` (enum test); it is built on
  the same GET-only `backend/srs/http.py`; a recording transport asserts every request during a
  mocked session that exercises all three tools is `GET`. If a community server was chosen, the
  spawn adds `--allowedTools` with that explicit read-only tool list and a test asserts
  `init.tools[]` contains no other MCP tool. Status: `init.mcp_servers[]` entry →
  `connected`/`failed`; each `tool_result` → `used` with ok/error and a timestamp.
- **Validate** — Live JLPT progress + ~15 recent/ghost grammar points. Confirm Claude can call
  the MCP tool mid-conversation — and that it does so on request or roughly every 15 minutes,
  **not** every turn (that is a latency regression; watch the timing log). Break the credential
  on purpose → chip reads `failed`, conversation continues.
- **Gate M4b** — Bunpro fully removed → startup unaffected, conversation unaffected, chip
  `disabled`; Bunpro broken → chip `failed`, conversation unaffected.

### 14. Profile renderer — `backend/srs/profile.py` — **M1** (tuned in M4)

- **Test** — Output is **≤ 600 tokens** with a full WaniKani + Bunpro payload (a hard assertion,
  since this is a latency ally per ADR-011). Renders correctly with zero, one, or both sources,
  and says which are present so the tutor does not guess. Written to `logs/profile-<date>.json`
  and rendered into `.cache/tutor_prompt_rendered.txt`.
- **Validate** — Read the rendered profile as prose. If it would not help a human tutor, it will
  not help this one.
- **Integrate** — Into `prompts/tutor.md` at `{{student_profile}}` at session start.
- **Gate M4c (acceptance)** — With a real token, ≥ 3 recent unlocks appear in a 5-minute
  conversation, verifiable in the logs.

### 15. Session log, redaction & summary — `backend/app.py` — **M5**

- **Test** — `logs/session-<timestamp>.jsonl` opens with a header record (resolved config with
  secrets redacted, §5b status table) and then holds one clean record per turn: user transcript,
  assistant text, stage timings, emotion(s), tool calls (name + sanitised args + ok/error).
  Parseable by a trivial reader script — it is the future Anki mine, so schema stability matters
  more than richness. **Redaction test (spec §11):** with fake tokens in the environment, run a
  session that triggers an MCP tool error and a WaniKani auth error; assert no `.env` value and
  no `Bearer …` string appears under `logs/` or `.cache/` except `.cache/mcp.json`.
- **Validate** — On 「さようなら」 the tutor gives a 3-sentence Japanese summary and the session
  closes cleanly.
- **Gate M5a** — A whole session round-trips through a reader script with no malformed lines;
  redaction test green; `make check-secrets` green on the tracked tree.

### 16. Service status registry — `backend/status.py` + `frontend/src/status.ts` — **M3 (UI) / M4 (real signals)**

**Contract:** each subsystem reports `(service, state, detail, last_error?)` to one registry; the
registry emits `service_status` on change and every 30 s, feeds `make doctor`'s table, and writes
the session-log header.

- **Test** — State set per service matches spec §5b exactly (enum test). Debounce: 100 rapid
  identical updates emit once; a change emits immediately; the 30 s heartbeat emits even without
  change. Sanitiser: a `last_error` containing a token or `Bearer …` is masked before it leaves
  the registry.
- **Validate** — Each chip driven to each of its states by real conditions (listed in
  subsystems 4, 5, 12, 13 and the `claude` cases: `rate_limited` needs a real rate-limit event or
  the V0.2 fixture replayed; `fallback` by forcing an overload path; `restarting` via the
  `settings.model` change).
- **Gate M4d** — Every state of every chip has been observed from a real signal at least once
  and screenshotted into the M4 acceptance notes.

### 17. Emotion pipeline (end-to-end) — spans chunker, emotions, avatar — **M2 (voice) / M3 (face)**

**Contract:** a tag in Claude's text changes the voice and the face of exactly the sentences it
covers, at the moment those sentences start playing (ADR-020).

- **Test** — Pipeline test with a stubbed TTS and a recorded stream: the turn
  `[happy]よくできました。[thinking]でも、ここは少し違います。[serious]もう一度言ってみてください。`
  produces three `speak` messages carrying `happy`, `thinking`, `serious`; the TTS stub sees the
  three matching style/param sets; the text sent to TTS contains no brackets. A tag-less second
  sentence inherits the first's emotion. A tag mid-sentence is left in place and logged.
- **Validate** — Scripted 4-sentence turn covering all four tags, on the real avatar and real
  voice: each visibly and audibly distinct, and the face changes with the audio, not before it.
  Then a normal 10-turn conversation: the tutor actually uses tags (count them in the log — if
  it never does, the prompt wording is wrong, not the pipeline).
- **Gate M3f** — The scripted turn passes by eye and ear; tags appear in ≥ 30 % of turns in a
  normal conversation.

### 18. Settings store & interface — `backend/config.py` + `frontend/src/settings.ts` — **M0 (store) / M3 (drawer) / M5 (full page)**

**Contract:** one schema drives `config.py`, `.env.example`, and the settings page. Resolution
is defaults → `settings.json` → env. Secrets never return to the browser in full (ADR-022).

- **Test** — Resolution order (a key set in all three sources resolves to env; in two, to
  `settings.json`; in none, to the default). `settings.json` is written atomically and with mode
  `0600` (skip the mode assertion on Windows, assert the atomic rename everywhere). A
  `settings` WS update with an invalid type is rejected with `error` and nothing is persisted.
  The echo of a secret is `{set: true, hint: "…" + last 4}` and never the value. Inventory test:
  keys in `config.py` == keys in `.env.example` == keys in the settings schema. First-run test:
  no `settings.json` → the server reports `first_run: true` in the initial `settings` echo.
  `settings_test` for each service dispatches to the real check function (stubbed) and results
  in a `service_status`.
- **Validate** — Fresh clone, **no `.env`**: start the app, enter tokens on the settings page,
  press each Test button, watch chips go green, hold a conversation. Change `model` and confirm
  the conversation memory survives the respawn. Restart the app and confirm everything persisted.
- **Integrate** — The `settings` / `settings_test` messages (subsystem 7) and the status
  registry (subsystem 16).
- **Gate M5b** — The M5 acceptance: clone → first conversation without ever creating a `.env`.

---

## Integration order

Each row is the seam introduced, and the one assertion that proves it.

| Step | Seam                                | Proof                                                                   | Milestone |
|------|-------------------------------------|-------------------------------------------------------------------------|-----------|
| 0    | GET-only client → SRS fetchers      | Recording transport sees only `GET`; client has no write attribute      | M1 |
| 0b   | Fetchers → profile renderer         | ≤ 600 tokens with both sources; clean with neither                      | M1 |
| 1    | REPL → Claude session               | Streamed deltas arrive; `apiKeySource == "none"`; `init.tools[]` has no built-ins; real profile in the prompt | M1 |
| 2    | Claude session → chunker            | Sentences + emotions emitted before the turn completes                  | M1 |
| 3    | Mic → VAD                           | Exactly one `speech_end` per utterance                                  | M2 |
| 4    | VAD → STT                           | Transcript within budget; hallucinations dropped                        | M2 |
| 5    | Chunker → emotions → TTS            | First WAV before generation ends; style/params match the tag            | M2 |
| 6    | TTS → viseme mapper                 | Timeline length matches WAV duration at every emotion speed             | M2 |
| 7    | Backend → WS → browser              | `speak` renders; protocol names match on both sides                     | M3 |
| 8    | Browser → TalkingHead               | Lip-sync visually tight; emotion applied at audio start                 | M3 |
| 9    | VAD ↔ playback (barge-in)           | Stop < 300 ms on headphones; zero self-interruptions on speakers        | M3 |
| 10   | Status registry → status bar        | Kill VOICEVOX → chip `down` within a turn                               | M3 |
| 11   | Settings drawer ↔ server            | `model` change respawns with `--resume`; conversation memory intact     | M3 |
| 12   | Profile → tutor behaviour           | ≥ 3 recent unlocks used in 5 minutes (prompt tuning on the M1 plumbing) | M4 |
| 13   | Claude ↔ Bunpro MCP                 | Tool called on request, not every turn; chip `used`                     | M4 |
| 14   | Everything → session log            | One clean JSONL record per turn; redaction test green                   | M5 |
| 15   | Settings page ↔ store ↔ services    | Clone → first conversation with no `.env`; Test buttons drive the chips | M5 |

---

## Standing regression suite

Runs from M2 onward, every milestone, before any gate is called met:

1. `make doctor` — clean on the target machine, including loopback-only and `git check-ignore`
   checks, and the status table printed.
2. `make test` — hermetic unit + golden tests, no network, no GPU. Includes the env-allowlist,
   `apiKeySource`, `.env.example`-vs-`config.py` inventory, and protocol contract tests.
3. `make check-secrets` — no token-shaped strings and no current `.env` value in the tracked
   tree.
4. **20-turn latency run** — p50/p90 reported; p90 ≤ 3.0 s.
5. **VRAM check** — steady state ≤ 10 GB with the browser open.
6. **Speakers run** — 10 turns on laptop speakers, zero self-interruptions.
7. **Degradation matrix** — the app starts, holds a conversation, and shows the right chips
   with: no WaniKani token, no Bunpro, neither, Bunpro credential broken, WaniKani offline with
   warm cache, VOICEVOX restarted mid-session, claude child killed mid-turn, a forced rate-limit
   fixture.
8. **Secret hygiene** — no `.env`, `settings.json`, `mcp.json`, GLB, model weights, or personal
   SRS data in `git status`; redaction test green, including `settings` echoes.
9. **Read-only guarantee (Golden Rule §0, ADR-021)** — `make check-readonly` runs first and
   passes; the recording-transport test over a full mocked session (session-start fetches,
   `resync`, all three Bunpro MCP tools) reports zero non-`GET` requests; the violation fixture
   tree is still fully caught. Any of these failing is a release blocker regardless of anything
   else being green.

---

## When reality contradicts this plan

Per spec §14: **ask, then update the spec.** Do not silently diverge. If a verification spike
returns something different from what the spec assumed — a renamed CLI flag, a changed VOICEVOX
field, a TalkingHead signature — record it in [ADR.md](ADR.md) as a new or superseding decision,
fix [ATAMA-AI_SPEC.md](ATAMA-AI_SPEC.md), then fix the code. In that order.
