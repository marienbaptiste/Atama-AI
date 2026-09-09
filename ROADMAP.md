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

| **V0.12** | **Context: how big is the window, and what does the provider do when it fills?** Drive one long real session, logging `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` per turn until the CLI compacts on its own. Answer: the usable window in tokens; how compaction announces itself on the stream (an event? silence?); **how long it stalls**; and whether the session id survives it. | Pinned constants: usable window, a safe `CONTEXT_ROTATE_AT` fraction under it, and the measured stall — the number that justifies ADR-032. Plus a fixture of whatever the stream emits. | Open — **blocks the rotation half of M4c.** The memory half (ADR-031) does not depend on it and ships first. |

| **V0.13** | **Is a Japanese-specialised STT model better on the student's own voice?** `kotoba-tech/kotoba-whisper-v2.0-faster` is **verified to exist** (2026-09-09): `library_name: ctranslate2`, with `model.bin`/`config.json`/`tokenizer.json`/`vocabulary.json`, so `WhisperModel("kotoba-tech/kotoba-whisper-v2.0-faster")` loads it directly — no conversion. Its card claims better CER/WER than `large-v3` in-domain (ReazonSpeech), only **"competitive"** out-of-domain, and 6.3x faster from the distil architecture (756M params, 2 decoder layers vs 1550M). A learner's accented Japanese is firmly out-of-domain, so the published numbers do not settle it. Record real utterances through the real mic+VAD path and read the transcripts side by side: `python -m backend.tools.stt_compare --record 6`. | A decision in a new ADR superseding ADR-004's model choice, or an explicit "stayed on large-v3 because". Plus pinned VRAM and median latency for whichever wins, and the clips kept in `logs/stt/` as the only STT regression corpus this project can have. | **Done 2026-09-09 — negative result.** `large-v3` stays; kotoba-whisper is a 756M distil model and there is no larger one. See findings log. |

### V0 findings log

**2026-09-09 — STT model comparison (V0.13), on the user's own voice:**

- **Result: `large-v3` stays.** Judged by ear on six recorded utterances through the real mic+VAD
  path: "large v3 is much better". Not close.
- **Why, almost certainly: it was not a fair fight on size.** `kotoba-whisper-v2.0` is a *distil*
  model — **756 405 760 params with 2 decoder layers**, against `large-v3`'s **1 543 490 560**. The
  card's "6.3x faster" and its "competitive out-of-domain" hedge are the same fact seen from two
  sides, and a learner's accented Japanese is the out-of-domain case. The published in-domain
  CER/WER win on ReazonSpeech (Japanese TV) did not transfer.
- **There is no larger kotoba model, and there will not be one.** Verified 2026-09-09 via the HF
  API: `kotoba-whisper-v2.0`, `v2.1`, `v2.2` and `bilingual-v1.0` are **all 756 405 760 params** —
  byte-identical model size. v2.1 and v2.2 add punctuation and speaker diarization as
  *post-processing* around the same distilled weights, not a bigger network. Distillation is the
  entire premise of the project, so "the latest kotoba at about the same size as large-v3" does not
  exist. `litagin/anime-whisper` is also 756M (and anime-domain, wrong register for a tutor).
- **Full-size Japanese ASR is a thin field.** Of the top 100 Japanese ASR models by downloads, only
  four are large-v3 class (>1.2B): `openai/whisper-large-v3` (1.54B), `Qwen/Qwen3-ASR-1.7B`
  (2.35B), `mistralai/Voxtral-Mini-4B-Realtime-2602` (4.43B), `microsoft/VibeVoice-ASR` (8.67B).
  **None is CTranslate2-compatible** — they are LLM-based ASR needing torch + transformers, which
  is an ADR-004 architecture change, not a model swap. Voxtral and VibeVoice are also out on the
  §10b VRAM budget before anything else loads.
- **Open, if STT is revisited:** `Qwen3-ASR-1.7B` claims 52-language support including Japanese and
  SOTA among open-source ASR. Worth a spike of its own *only* with eyes open about the cost: a
  heavy new dependency, ~4-5 GB against the 10 GB cap, and — the real unknown — per-utterance
  latency, since our workload is many short utterances and an autoregressive decoder with
  `max_new_tokens` is a different latency profile from Whisper's. Do not start it without measuring
  that first.


**2026-09-09 — M2b, part 1: does the startup warm-up earn its place? Yes.** Same six clips,
replayed twice in separate processes (`large-v3` @ `float16`), once loading the model and going
straight to work, once doing the 1 s dummy transcribe first:

| | first clip | median of the rest | p50 | p90 |
|---|---|---|---|---|
| no warm-up | **413.5 ms** | 276.4 ms | 299.1 ms | **388.0 ms** |
| with warm-up (419 ms at startup) | **235.5 ms** | 283.4 ms | 280.0 ms | **343.0 ms** |

- **The first-turn penalty is real and the warm-up removes it.** The identical clip costs 413.5 ms
  cold and 235.5 ms warm — ~140 ms above the median, paid by the student's first sentence, which is
  the worst possible place for it. 419 ms at startup buys it back. This is the third instance of the
  same bug shape in this codebase (VOICEVOX styles, the output stream, now Whisper): *a subsystem
  that reports ready before it can actually work*. Only this one was already fixed.
- **p90 is 343 ms against the 350 ms budget — but this is NOT gate M2b.** Six clips cannot produce a
  p90, the margin is 7 ms, and the roadmap asks for **20 recorded utterances at the user's own
  speaking level**. Treat this as evidence the budget is reachable, not as evidence it is met.
- Still outstanding for M2b: the 20-utterance run, and zero hallucinated transcripts across a
  2-minute silent recording. Neither can run until the microphone is unmuted.

**2026-09-09 — quantisation (V0.13 addendum), replayed on the same six clips:**

| | VRAM | median | utt_02 | utt_06 |
|---|---|---|---|---|
| `large-v3` @ `int8_float16` | 2 178 MiB | 290 ms | ここ**　**もらったことはなぜ? | **ショッキング**するのがいいよ |
| `large-v3` @ `float16` | **3 880 MiB** | **287 ms** | ここ**を**もらったことはなぜ? | **貯金**をするのがいいよ |

- **`float16` is now the default.** It is *not slower* — 287 ms against 290 ms, within noise —
  because int8 buys nothing on a GPU that runs fp16 natively; the quantisation was paying accuracy
  for a saving that never existed at inference time.
- The transcripts are the point, not the milliseconds. At int8 the model heard ちょきん (貯金,
  savings) as **ショッキング**, and dropped the particle を in utt_02. At fp16 both are right.
- Cost: **+1 702 MiB**, to 3 880 MiB against the §10b 10 GB cap. Headroom we were not spending.
- `int8_float16` stays as the documented step-down if VRAM ever gets tight, ahead of dropping to
  `medium` — now with a measured accuracy price attached to it rather than an assumed free lunch.
- Replay any future STT change against these clips before believing it:
  `python -m backend.tools.stt_compare --replay --models large-v3@float16,large-v3@int8_float16`

**The cheaper levers on `large-v3`, not yet tried, in order:**

1. ~~**Stop quantising.**~~ **Done 2026-09-09 — `float16` is now the default.** See the addendum
   below: better transcripts, identical latency, +1.7 GB of headroom we were not spending.
2. **Prime the decoder with the student's own vocabulary.** `condition_on_previous_text=False` is
   deliberate (spec §9 — it stops hallucination loops), but it means every utterance is transcribed
   cold. The WaniKani unlocks and Bunpro grammar in the profile are exactly the words this student
   is most likely to say. faster-whisper's `initial_prompt`/`hotwords` are the mechanism —
   **verify the parameter against the installed version before pinning** (ADR-015).
3. **`beam_size`** is 5. Raising it trades latency for accuracy; measure both.

The six clips are in `logs/stt/` (gitignored). They are the regression corpus: any future change
to STT gets replayed against them with `--replay` before it is believed.


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
(§10) — over, and that measurement is what moved the `--effort` default. Latency is
M3's gate, not M1's; the numbers are recorded here so the M3 work starts from data.

**2026-09-09 — first lever pulled: `--effort` default high → medium.** The A/B above is the
whole argument: medium is the fastest of the three on *both* numbers (2.14 s between turns
vs high's 3.75 s; 5.8 s opening vs 19.2 s), so `low` is not a further step down — it is
slower than medium at less effort, and buys nothing. Remaining levers for M3: model and
prompt size.

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
- **`/version` answering is NOT readiness.** The engine loads a style's model on first *use*, so a
  green status chip could still be followed by a silent first sentence — indistinguishable, to the
  student, from a hang. `POST /initialize_speaker?speaker=<style_id>&skip_reinit=<bool>` -> 204 and
  `GET /is_initialized_speaker?speaker=<style_id>` -> bool fix that. Measured (style 53): forced
  reinit **534 ms**, `skip_reinit=true` on an already-loaded style **2 ms**, so warming at startup is
  free once warm and idempotent. `speaker` is a STYLE id, so *every distinct style in the emotion
  table* needs its own call — warming only the default leaves the first emotional sentence cold.
  Startup now warms all of them before Sensei's opening line, and `voicevox` gained `loading`/`warm`
  states so "ready" on screen means "can speak now" (spec §5b).
- `GET /speakers` -> `[{name, speaker_uuid, styles: [{id, name, type}], supported_features, version}]`,
  43 speakers. **Styles are resolved by NAME**, not id, so an engine upgrade renumbering ids is safe.
  Chosen default: **No.7** — ノーマル=29, アナウンス=30 (crisp/formal), 読み聞かせ=31 (warm read-aloud):
  a good spread for a teacher.
- Synthesis of one short sentence takes **470-740 ms** on CPU. The §10 budget for "VOICEVOX first chunk
  + WS delivery" is 400 ms, so this is over already, before any WebSocket. Levers for M3: shorter first
  sentences, and starting synthesis on the first sentence while the rest still streams (already done).

**2026-09-10 — M2c, first-chunk synthesis on the target box (speaker 53, CPU VOICEVOX):**

| chars | synth | audio | RTF |
|---|---|---|---|
| 3 (はい。) | 245 ms | 544 ms | 0.45 |
| 6 (こんにちは。) | 309 ms | 864 ms | 0.36 |
| 8 | 358 ms | 1237 ms | 0.29 |
| 10 | 431 ms | 1653 ms | 0.26 |
| 14 | 523 ms | 2155 ms | 0.24 |
| 19 | 682 ms | 3083 ms | 0.22 |

Over 60 realistic tutor sentences x 5 emotions: **p50 493 ms, p90 622 ms, max 699 ms.**

- **The 0.40 s stage budget is NOT met at p90.** Synthesis is linear at ~35 ms/char and the
  levers are gone: ADR-005 forbids GPU VOICEVOX, and speaker 53 was already chosen as the
  *cheapest* of the V0.3 shortlist (~615 ms/sentence, pinned then). Only a short first sentence
  comes in under 400 ms — 3-8 characters does, 10+ does not.
- **The N/N+1 overlap condition is met structurally, not by luck.** The real-time factor is
  0.22-0.45, so synthesis of the next sentence always finishes well before the current one stops
  playing, and the margin *widens* with length. The queue in `speaker.py` already works this way.
- **But the §10 stage table predates push-to-talk.** With `TURN_MODE=ptt` the end-of-speech stage
  is 0.00 s, not 0.50 s. Re-adding the measured numbers: 0 (VAD) + 0.34 (STT p90) + 1.60 (Claude)
  + 0.62 (TTS p90) + 0.15 (playback) = **2.71 s**, inside the 3.0 s hard gate. The stage that
  overspends is covered by the stage that no longer exists.
- **Open for the user:** rebalance §10's stage table for ptt (moving the VAD allocation to TTS
  would make M2c pass honestly), or leave the table and record M2c as missed-but-compensated.
  Either way the hard number that matters — voice->voice p90 <= 3.0 s — is not yet measured
  end to end; that is M3d.

**2026-09-10 — M2d, measured against the live engine at every emotion speed:**

`postPhonemeLength` **is** divided by `speedScale`, like `prePhonemeLength`. Controlled test at
speaker 53, `pre=0`: post 0.0->0.5 adds 0.5013 s at speed 1.0 and **0.2453 s at speed 2.0**. The
mapper already divides both, so its arithmetic is correct.

| emotion | speed | mean err | max abs err |
|---|---|---|---|
| neutral | 1.0 | +13.2 ms | 24.3 ms |
| happy | 1.05 | +0.3 ms | 13.2 ms |
| thinking | 0.95 | -5.8 ms | 19.9 ms |
| surprised | 1.1 | -9.5 ms | 23.3 ms |
| serious | 0.95 | -5.8 ms | 19.9 ms |

- **The residual is VOICEVOX's, not ours.** At speed 2.0 the engine returns 0.3627 s where exact
  division of the speed-1 mora time would give 0.3520 s — **10.7 ms longer**, because it rounds
  every mora to sample boundaries. No change to `visemes.py` can remove that; matching it exactly
  would mean reimplementing the engine's rounding.
- **The drift is text-dependent, and bigger than the live sweep suggested.** The committed
  fixtures span **-11.4 ms** (さしすせそ) to **+46.4 ms** (ぱぴぷぺぽ) — re-synthesised from the
  exact stored queries, so the recorded WAV lengths are not stale. It is not the consonants
  either: an all-vowel あいうえお is +5.1 ms and か-row is *negative*. There is no function of the
  audio_query that recovers it.
- **So the drift is removed, not tolerated.** `VisemeTimeline.fitted_to(wav_ms)` scales the
  predicted timeline onto the real WAV, which the caller has the instant synthesis returns.
  Cumulative lag that was worst at the end of a sentence — where it shows — becomes a
  proportional stretch of a fraction of a frame per viseme. Re-measured across all five emotions:
  **0.0 ms**. `build()` remains pure and remains a prediction; the fit is a separate pure
  function, so spec §7's "keep them pure" still holds.
- Tests split accordingly: `MAX_PREDICTION_DRIFT_MS = 50` bounds `build()` alone (loose on
  purpose — tightening it would only assert VOICEVOX's quantisation), while the fitted timeline
  is asserted exact.

**2026-09-09 — voice stability, measured (V0.3 follow-up):**

The user reported たなか's voice as "unstable between phonemes". Measured rather than guessed, on
a 7-sentence turn synthesised through the real pipeline: F0 tracked by autocorrelation at a 5 ms
hop, jitter = mean frame-to-frame |ΔF0| as a fraction of mean F0 (reported in cents, so registers
compare), flux = mean frame-to-frame L2 change of the normalised magnitude spectrum.

| speaker | median F0 | jitter | flux | synth/sentence |
|---|---|---|---|---|
| 麒ヶ島宗麟 53 (was たなか) | 152 Hz | 33.4 cents | 0.0724 | ~530 ms |
| **黒沢冴白 100 (now たなか)** | 155 Hz | **20.2 cents** | **0.0456** | ~840 ms |
| 玄野武宏 11 | 145 Hz | 24.6 cents | 0.0439 | ~770 ms |
| No.7 29 (みなみ) | 240 Hz | 23.8 cents | 0.0674 | — |

- The wobble is **not a concatenation seam**: splitting ΔF0 by mora boundary (boundaries taken from
  the `audio_query` mora durations) gave 2.95 Hz *at* boundaries vs 3.05 Hz *inside* moras for 53.
  It is the model being unsteady throughout, and at 152 Hz a 3 Hz wobble is far more audible than
  the same 3 Hz at No.7's 240 Hz.
- **No engine parameter fixes it.** Sweeping `intonationScale` 1.0 -> 0.5 (which flattens the
  prosody to a monotone) moved jitter only 36.8 -> 34.2 cents; `speedScale` and `pitchScale` were
  the same story. Post-hoc F0 correction (TD-PSOLA) would work but needs a per-sentence pass in a
  stage already over budget, so it was rejected.
- **`SINGLE_STYLE_SPREAD` was making it worse.** Widening `intonationScale` stretches the F0 contour
  and the model's own wobble with it: at the spread's 1.54, jitter went 2.15% -> 2.41% and flux
  0.0773 -> 0.0821. Split into `SINGLE_STYLE_PITCH_SPREAD` (1.8, kept — a constant log-F0 offset
  carries the emotion at no stability cost) and `SINGLE_STYLE_INTONATION_SPREAD` (1.0, i.e. none).
- **The steadiest voice is not the right voice.** 53 was swapped to 黒沢冴白 (100) on the numbers,
  then swapped back after listening: on a fifty-year-old ex-engineer the unsteadiness reads as age.
  53 stays for `tanaka`, deliberately. This is why the selection rule ends in a human (ADR-030) —
  a gate that maximised the metric would have discarded the right answer.
- 玄野武宏 11 has four styles (ノーマル/喜び/ツンギレ/悲しみ), but only 喜び matches a name in
  `DEFAULT_TABLE`, so `surprised` and `serious` still fall back to ノーマル — the multi-style
  advantage is mostly unrealised, and he is slower than 53 anyway.

**Full sweep, both registers** (every speaker's base style, one fixed sentence, 2026-09-09).
Male/low register, 11 voices; female/high register, 30 voices. Best of each, and what was chosen:

| register | best measured | jitter | chosen for a persona | jitter | why not the best |
|---|---|---|---|---|---|
| male | 白上虎太郎 12 | 19.9 | 麒ヶ島宗麟 **53** (`tanaka`) | 33.4 | unsteadiness reads as age, and cheapest to synthesise |
| male | — | — | 栗田まろん **67** (`hayashi`) | 22.0 | right age, and fastest of the shortlist (~643 ms) |
| female | 雨晴はう 10 | 11.1 | No.7 **29** (`minami`) | 20.2 | the top female voices are 300+ Hz anime registers, wrong for a 40-year-old |
| female | — | — | 冥鳴ひまり **14** (`mori`) | 15.3 | steadiest voice that still sounds nineteen |

- **No.7 (29) came last of all 30 female voices** on jitter and was kept anyway, by ear. Worth
  knowing if みなみ is ever reported as rough — it is the known-weakest of the four.
- **The catalogue has a ceiling.** The top four male voices sit within 2 cents of each other and
  the top female voices within 2. The residual synthetic quality the user still hears after the
  swap is therefore the **vocoder**, not the speaker — no choice inside VOICEVOX addresses it.
  Confirmed negative on the obvious explanations: the intended F0 contour is rich, not a staircase
  (25 distinct pitches over 25 moras, 11.8 semitones); rendered range 9.9 st vs the reference's
  9.0; periodicity 0.62, *lower* (less buzzy) than No.7's 0.79; mora timing is not metronomic
  (duration CV 28-41% across the field).
- **Open, if naturalness ever becomes a gate:** replace the engine. The candidate is
  **AivisSpeech**, believed to expose a VOICEVOX-compatible HTTP API over Style-Bert-VITS2 models —
  which would keep `audio_query`/`synthesis` and, critically, the mora timings the viseme pipeline
  depends on. **Unverified**: per ADR-015 this needs its own spike against the live `/docs` before
  anything is pinned, and it likely wants GPU, which collides with Whisper's §10b budget. This is
  an ADR-005 supersede conversation, not a swap.
- **Rejected: a DSP repair pass.** An EQ-style filter cannot touch F0 jitter — the whole harmonic
  stack moves together, and an LTI filter has a fixed response, so it would convert pitch
  instability into amplitude artifacts. WORLD or TD-PSOLA resynthesis *would* work, and VOICEVOX
  hands us the intended per-mora F0 so the target contour is known rather than guessed. Not built:
  the voice swap already solved the reported wobble, and this stage is 2x over its 400 ms budget.
  Revisit only if a persona is forced back onto a rough voice for latency reasons.

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
- **Silero v5 must be fed 576 samples, not 512.** It prepends a 64-sample *context* — the tail of
  the previous frame — to every window. The ONNX graph accepts a dynamic width, so passing 512
  raises nothing: it simply returns ~0.002 for everything, including loud, clear speech. The
  symptom is a level meter that moves beautifully while nothing is ever detected, which reads as
  a microphone problem and is not one. Measured on one synthesised Japanese sentence:
  512-wide -> max probability 0.003, nothing detected; 576-wide -> 1.000, 62 of 81 frames.
  Regression-tested two ways: a spy asserts the model receives 576 and that the context really is
  the previous frame's tail, and a committed 16 kHz speech fixture must yield a full
  speech_start/speech_end at both full volume and at 0.05x (a quiet headset's real level).
- **A forming utterance must tolerate dips.** Requiring `min_speech_ms` of *uninterrupted*
  above-threshold frames means "300 ms with no dip at all", which real speech never satisfies —
  plosives and syllable gaps dip constantly. Reset only after a sustained gap
  (`onset_tolerance_ms`, 200 ms).
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
  **DEFERRED 2026-09-10, user directive.** `TURN_MODE=ptt` is now the default and the key decides
  when a turn ends, so this gate measures the fallback mode rather than the one in use. It does
  not block M2. Finish it near the end, when the look and feel is settled and the silence window
  can be tuned against real sessions instead of a staged three minutes.
  Note what ptt also removes: the 900 ms window leaves the §10 budget entirely (a third of the
  3.0 s), and gate **M3b**'s "zero self-interruptions on speakers" is close to vacuous when the
  tutor's own voice cannot end a turn. Neither gate is deleted — both are re-measured if `vad`
  ever becomes the default again.

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
- **Gate M2d** — **MET 2026-09-10, exactly: 0.0 ms** at every emotion speed, five sentences each.
  No tolerance argument required, because the drift is removed rather than tolerated —
  `VisemeTimeline.fitted_to(wav_ms)` stretches the predicted timeline onto the real WAV in
  `tts_voicevox.say()`. `build()` stays pure and stays a prediction (spec §7); the fit is a
  separate pure function applied where the ground truth exists.

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

### 18. Memory and context — `backend/memory.py` + `backend/session.py` — **M4**

Cross-session recall (ADR-031) and pre-emptive session rotation (ADR-032). Spec §6b. Both exist to
serve one rule: **nothing that is not speech goes on the critical path.** Everything here runs at
session start, at session end, or in the *speaking gap* — the seconds after `TurnComplete` while
the avatar is still playing audio and the orchestrator is idle.

- **Build order.** (a) turn log first — it is the input to everything else and to the Anki mine;
  (b) start-of-session read into the prompt; (c) end-of-session summariser; (d) rotation last,
  because it depends on the log and is the only piece that can break a live conversation.

- **Test — turn log.** Schema contract test against the §6b shape, asserting field names and that
  every value is JSON-serialisable; append-only test (a second session appends, never rewrites);
  a crash mid-write leaves the file parseable up to the last complete line. Hermetic.
- **Test — prompt assembly.** Memory sections respect `MEMORY_MAX_TOKENS`, truncate at a line
  boundary, and are reported in `RenderedPrompt.truncated` — the same golden machinery as soul and
  profile. Absent memory files degrade to no section, never to an error or an empty heading.
- **Test — the critical path is clean.** The strongest test in this subsystem: drive a full turn
  with fakes and assert that **no memory read, write or rotation call occurs between speech-end and
  first audio**. Implement it by recording call timestamps against the turn's phase, not by mocking
  and hoping. This is the regression that matters; the rest is detail.
- **Test — best-effort.** A memory write that raises, hangs, or is cancelled mid-flight must not
  fail the turn, and must leave the log parseable. Simulate the student barging in during a write
  and assert the write is abandoned and the turn proceeds.
- **Test — summariser.** Runs off a fixture turn log, never a live conversation; produces output
  within budget; a failed summarise leaves the previous brief intact rather than truncating it to
  nothing. Deterministic given the same log.
- **Test — rotation, hermetic.** With a fake brain reporting rising `usage`, assert: rotation arms
  above the threshold; the swap happens only at a turn boundary; a not-ready replacement keeps the
  old session and retries; the old process is closed only after the new one has taken a turn; a
  crash mid-rotation resumes the *authoritative* session (the old one until the swap lands).

- **Validate live.** A 40-minute lesson that crosses the rotation threshold at least once. Watch
  for: a visible or audible seam at the swap; the tutor forgetting something it should still know;
  any turn slower than usual attributable to rotation. Then close the app and reopen it — the tutor
  must open from the last-session brief, and `student.md` must contain something a human would agree
  is worth remembering.
- **Validate the fallback.** Force rotation to fail (make the second spawn error) and confirm the
  conversation continues on the old session, the provider's own compaction is logged as a latency
  event, and nothing crashes.

- **Gate M4c** — Across a 40-minute lesson: zero compaction stalls visible to the student; p90
  voice→voice unchanged within noise between turns before and after a rotation; the turn log parses
  100% and matches the schema; a fresh launch demonstrably recalls the previous session.

**Known risk.** Rotation is the only feature in this milestone that can break a working
conversation, and its failure mode is subtle — a tutor that quietly forgets. Ship the log, the
start-of-session read and the summariser first; run them for several real lessons before enabling
rotation. `CONTEXT_ROTATE_AT` unset means "never rotate", which must remain a supported
configuration.

### 19. Settings store & interface — `backend/config.py` + `frontend/src/settings.ts` — **M0 (store) / M3 (drawer) / M5 (full page)**

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
9. **Critical path is speech only (ADR-031/032)** — the recorded-phase test proves no memory
   read, write, summarise or rotation call falls between speech-end and first audio. A 40-minute
   run shows zero compaction stalls and no p90 regression across a rotation.
10. **Read-only guarantee (Golden Rule §0, ADR-021)** — `make check-readonly` runs first and
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
