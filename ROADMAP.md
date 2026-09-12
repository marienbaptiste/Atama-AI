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
1b. **Milestone order, 2026-09-10 (user, ADR-034):** M2 declared done — M2a, M2b's 2-minute silence
   check and M2c's overlap and five-emotion checks are **deferred to the end of the project, not met**.
   **M4 is built before M3.**
2. **The two hard numbers are gates, not aspirations** — voice→voice p90 ≤ 5.0 s (was 3.0 s until 2026-09-10, ADR-033), VRAM ≤ 10 GB.
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
| **V0.4** | TalkingHead README: `speakAudio` signature; whether `vtimes`/`vdurations` are **ms or s**; the real mood name set; whether gestures exist and their names; the facility for overriding ARKit blendshapes directly (needed for `surprised`/`serious`/`thinking`). | Pinned constants + comment. A wrong timing unit is silent drift; a wrong mood name is a silent no-op. | **Done 2026-09-10** — against the README and `modules/talkinghead.mjs` 1.4: `speakAudio(audio, opt, onsubtitles)`, **all times in ms**, bare Oculus viseme ids, the closed mood set (no thinking/surprised/serious — those drive `neutral` + `setFixedValue` blendshape overrides), the gesture names, eye-contact defaults 0.2/0.5. Pinned in `constants.py` (TALKINGHEAD_*). Status corrected here 2026-09-11. |
| **V0.5** | WaniKani `/v2/user` and `/v2/assignments`: real response shape, pagination, rate-limit headers. | Sanitised fixtures (no personal data beyond what the golden tests need). | **Done 2026-09-09** — 6 endpoints captured live, fixtures in `backend/tests/fixtures/wanikani/`. See findings log. |
| **V0.6** | Baseline VRAM: load faster-whisper `large-v3` @ `int8_float16` alone, read `nvidia-smi`. | A number. If > ~4.5 GB, ADR-004's fallback triggers now, not at M5. | **Done 2026-09-09 — 2 169 MiB.** Comfortably under the 3.5 GB estimate and the 4.5 GB fallback threshold: `large-v3` stays, `medium` is not needed. |
| **V0.7** | **Does a trustworthy Bunpro MCP server exist?** Survey community stdio MCP servers for Bunpro; check they work against the current site/API, what credential they take, whether the code is small enough to read end-to-end (it receives your credentials), and — **disqualifying** — whether it exposes any write tool that cannot be removed from the surface (ADR-021). If none passes, the decision is to write `backend/srs/bunpro_mcp.py` (spec §5) with read tools only. | A decision recorded in ADR (new entry), plus either a pinned version or a stub module. | **Done 2026-09-09** — decision: **write our own** (ADR-023). See findings log. |
| **V0.9** | WaniKani token permissions: confirm from `/v2/user` which fields expose the token's granted permissions, so `make doctor` can warn on a write-capable token (ADR-021). | Pinned field name + a sanitised fixture for both a read-only and a write-capable token. | **Done 2026-09-09 — negative result.** `/v2/user.data` keys are `current_vacation_started_at, id, level, preferences, profile_url, started_at, subscription, username`; **token scopes are not exposed** and probing them would require a write. Read-only scope can only be guaranteed at token creation; the doctor and the settings page *instruct*, they cannot verify. Test pins the absence. |
| **V0.8** | Do MCP tools survive `--tools ""`? Spawn with the Bunpro MCP configured and `--tools ""`; check `init.tools[]` for the MCP tool names. | Pinned: either `--tools ""` stands, or the fallback `--disallowedTools` list of the 20 built-in names from `init.tools`. | **Done 2026-09-09 — `--tools ""` stands**, *provided the MCP server is connected before the first turn*. See findings log ("Claude subprocess, live"). The disallow fallback is retired (tool names vary by platform). |
| **V0.10** | — | — | **No spike was ever recorded under this number** (checked against the whole git history, 2026-09-12): the numbering jumped from V0.9 to V0.11 when ADR-025/028 were written. Row kept so the gap is explained rather than silent. |
| **V0.11** | SearxNG's JSON API (`/search?format=json`, `language`, `categories`): verify the response shape against a running instance before writing the search MCP client (ADR-028; ADR-015 forbade coding it blind — on 2026-09-09 no instance was reachable and the Docker daemon was down). | A pinned request/response shape, the `search_mcp.py` client, a fixture. | **Done 2026-09-09, multi-source 2026-09-10** — verified live once the container was up: `backend/search_mcp.py` (one read tool, `search`; `language` ∈ {ja, en, all}), results interleaved round-robin across engines, Yahoo! JAPAN RSS merged as one provider, NHK excluded (frozen feed). Spec §5c, `test_search_mcp.py`. This row was never added to the table when ADR-028 cited it; restored 2026-09-12. |

| **V0.12** | **Context: how big is the window, and what does the provider do when it fills?** Drive one long real session, logging `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` per turn until the CLI compacts on its own. Answer: the usable window in tokens; how compaction announces itself on the stream (an event? silence?); **how long it stalls**; and whether the session id survives it. | Pinned constants: usable window, a safe `CONTEXT_ROTATE_AT` fraction under it, and the measured stall — the number that justifies ADR-032. Plus a fixture of whatever the stream emits. | **Closed differently, 2026-09-11** — the user rejected a one-off measurement: where the CLI compacts is provider policy and can move under us. Verified live (CLI 2.1.159, claude-sonnet-5): the **window** is reported every turn (`modelUsage[].contextWindow` = 200000 here, though the docs list Sonnet 5 at ~1M); compaction **announces itself** — `system/status "compacting"`, then `system/compact_boundary {trigger, pre_tokens, post_tokens, duration_ms}` (pinned in constants.py, fixtures in test_brain_claude_cli.py); the **stall** was 11.9 s for a manual 24.5k→0.8k compaction; the **session id survives** it. **No command reports where it will compact on its own** (`/context` answers locally with usage and window only; the documented `autoCompactWindow` and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` did not trigger it at 24.5k). So the point is watched for, not measured: rotation runs at a fraction of the reported window, and an `auto` compaction that beats it lowers that fraction for good (`session.learn`, `.cache/compaction.json`) — ADR-032 amendment. |

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
  Docker 29, a 16 GB RTX-generation laptop GPU), not WSL2. Everything so far is platform-neutral; §15's WSL2
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
parent's values. Process-environment overrides now require the `ATAMA_` prefix (the `.env` file
that then kept bare names has since been removed altogether — ADR-022 amendment, 2026-09-12).

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

**2026-09-10 — V0.4, TalkingHead verified (README + modules/talkinghead.mjs):**

- `speakAudio(audio, [opt={}], [onsubtitles=null])`, where `audio` carries `visemes[]`,
  `vtimes[]`, `vdurations[]`. **All times are MILLISECONDS.** This was the dangerous unknown —
  seconds would have been a silent 1000x drift — and our `as_message()` already emits ms
  (`vtimes[0] == 100.0` for a 0.1 s prePhonemeLength). **No conversion needed at the WS boundary.**
- `visemes[]` takes **bare** Oculus ids (`aa`, `PP`), not the `viseme_`-prefixed morph names.
  TalkingHead's set is 15; our mapper emits 14 of them and never emits anything outside it —
  the only one missing is `TH`, which Japanese has no sound for. Checked programmatically, not
  by eye.
- **Moods are a closed set of 8:** `neutral, happy, angry, sad, fear, disgust, love, sleep`.
  Three of our four tags — `thinking`, `surprised`, `serious` — **are not moods**, and an unknown
  name is a silent no-op. Spec §8's table already routes them to `neutral` + blendshape
  overrides, so the guess it was carrying turns out to be right; it is now verified rather than
  assumed.
- Blendshape control: `head.setFixedValue("jawOpen", 1)`, released with `null`; or an `anim`
  object `{dt: [ms], vs: {shape: [values]}}` passed to `speakAudio` for audio-synced motion.
- Gestures exist: `handup, index, ok, thumbup, thumbdown, side, shrug`, left-handed unless
  `mirror` is set.
- **Avatar requirement: full-body GLB, Mixamo-compatible rig, ARKit (52) + Oculus visemes (15).**
  Avaturn Type-2 avatars are stated compatible. **Still open:** the README does not give the
  Ready Player Me URL parameters that guarantee both blendshape sets in the export, and it is
  exactly the kind of thing that silently produces a face that cannot move. Confirm against Ready
  Player Me's own docs before the user downloads one — do not guess it here.

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

### 1. Claude session — `backend/brain/claude_cli.py` (behind the `Brain` interface, ADR-027) — **M1**

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
  cwd is used on restart. Timeout test: a turn that never produces `result` is **interrupted**
  (the stdin `control_request`/`interrupt` of ADR-037, never a signal) at the configured deadline
  and surfaces the apology line; a fake that ignores the interrupt is killed as a tree and resumed
  (`test_brain_claude_cli.py`). `init.session_id` is asserted equal to the id passed, on `--resume`
  too (verified live 2026-09-12).
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
  strips exactly one leading emotion tag — the seven of `chunker.EMOTIONS`, case-insensitive; a
  TalkingHead mood name in brackets is stripped and recorded as stray, `[neutral]` resets —
  **at turn start and at any sentence start**, attaching it to that sentence; a sentence without a tag inherits the
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
  The tests are hermetic: `test_vad.py` injects a spy session in place of the Silero ONNX model
  (the two tests that need the real model skip when `.cache/models/silero_vad.onnx` is absent).
- **Validate** — Live mic in a real room. Tune the silence window against actual conversational
  pauses; confirm the 0.50 s VAD budget line holds.
- **Integrate** — Upstream of STT; also the barge-in trigger (subsystem 8) and the listening
  reactions (subsystem 9).
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
  2-minute silent recording. **Amended 2026-09-10 (user, ADR-033):** the timing is accepted at its
  measured p90 of 0.58 s (large-v3, beam 5 — not traded for speed) and reported, not gating; the
  gate is the zero-hallucination check.

### 5. TTS client — `backend/tts_voicevox.py` + `backend/emotions.py` — **M2**

**Contract:** `(sentence, emotion) → (wav_bytes, audio_query_json)`.

- **Test** — Against a stubbed HTTP layer: `audio_query` then `synthesis` are called in order
  with the **emotion's** style id; the emotion table's `speedScale`/`pitchScale`/
  `intonationScale` reach the query; neutral uses the base style and the config defaults; a mapped
  style id missing from the `/speakers` fixture degrades to base style + scalars with a warning;
  a VOICEVOX 5xx or timeout degrades to a logged error, a skipped chunk, and a `voicevox=down`
  status — never a dead turn. An engine unreachable at startup leaves the table unresolved and
  `ensure_table()` rebuilds it on the first successful call; single-style pitch widening applies
  only when the catalogue confirms one style (`test_tts_voicevox.py`). The speech queue —
  synthesise N+1 while N plays, barge-in flush — has its own hermetic tests in `test_speaker.py`.
- **Validate** — Live engine: measure first-chunk synthesis + WS delivery against the 0.40 s
  budget for a typical ≤ 25-character sentence. Confirm the engine is on CPU (`nvidia-smi` shows
  no VOICEVOX process). Listen to the same sentence under all five emotions; tune the table by
  ear until each is distinct without being cartoonish.
- **Integrate** — Chunker → TTS, one sentence at a time, **overlapping** the next sentence's
  generation.
- **Gate M2c** — First-chunk audio ≤ 0.40 s p90; sentence *N* synthesises while sentence *N+1*
  is still being generated (visible in the timing log); five emotions audibly distinct.
  **Amended 2026-09-10 (user, ADR-033):** first-chunk timing accepted at its measured p90 of
  0.59 s and reported, not gating; the gate is the overlap and the five distinct emotions.

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

**Status 2026-09-11 — Gate M3a met.** `frontend/src/protocol.gen.ts` is *generated* from
`backend/models.py` (`python -m backend.tools.gen_protocol`) — every type, field and literal, not
just the names — and `test_models.py` fails while the committed copy is stale. The same file
round-trips every message, checks that an unknown client type is answered with an `error` while
the socket lives, that `stt_partial` is never emitted, and that every emotion the voice knows the
face knows. On the page, `ws.ts` `Handlers` requires one handler per server type, so `tsc` (run by
`npm run build`) fails on a missing one. One field was added at M3: `state.turn`, the barge-in
epoch (subsystem 8). **Not yet validated live:** kill the server mid-session and watch the page
reconnect (the watchdog and the retry are ported unchanged from the prototype, where they worked).

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
- **Integrate** — The page stops on the key event and the server confirms with `bargein`
  (closing the turn epoch); the brain's turn is interrupted with the control request of ADR-037;
  state-gated thresholds from subsystem 3 in `vad` mode.
- **Gate M3b** — Headphones: speech stops in **< 300 ms**, 10/10, no stale audio after.
  Speakers: **zero** self-interruptions in 10 turns.

**Status 2026-09-11 — built, not measured.** Found while building it: the server never sent
`bargein` and every `speak` said `turn: 0`, so the page played on to the end of the sentence it
had. Now the orchestrator keeps a turn epoch (`Hub.epoch`: up when a turn starts and when she is
interrupted), every `state` and `speak` carries it, and `bargein` closes it; the page drops any
sentence of a closed epoch however late it arrives, including one still decoding
(`speech.test.ts`, `test_app.py`). Push-to-talk: the page stops her on the key event itself
(`mic.ts`), before the server hears of it, and logs key-to-silence for every barge-in with the
session's worst — that log line is the M3b measurement. Hands-free: the server VAD decides (its
thresholds, subsystem 3) and the page stops on `bargein`. The `getUserMedia` constraints test does
not apply while the orchestrator captures the microphone (spec §2 "where the build stands"). Both
live runs — 10 interruptions on headphones, 10 turns on speakers — are **not yet run**.

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

**Status 2026-09-11 — the frontend is built.** `frontend/` (Vite 8, TypeScript 7, Vitest 5, no
framework): `index.html` + `src/{main, ws, protocol.gen, avatar, speech, rig, rigpanel, mic, status,
settings, ui}.ts`. Ported from the prototype: the start screen, the settings panel and its tabs, the
status bar with the service card, the mood kaomoji, the new-topic and stop buttons, the live tutor
switch, the device meter, the reconnect watchdog, the "tidying her notes" line, and the rig panel
(collapsed in the Activity card). New: subtitles (JP / off — her sentence as its audio starts, yours
once heard), **listening reactions** (attentive once you are really talking, a nod at each pause
≥ 300 ms, at most one per 1.5 s, some with a silent closed-mouth "mm"), the session timer, and the
headphones hint (hands-free only, until the first barge-in). **The emotion is now applied by the
audio-start callback** — TalkingHead's subtitle hook, which fires when a sentence leaves its queue
(talkinghead.mjs 1.4, read 2026-09-11) — where the prototype applied it on receipt, a whole
sentence ahead of the voice. `/` serves `frontend/dist` and is the only page — the prototype
`preview.html` was removed on 2026-09-11 (user). `make run` / `.\run` builds the page first when its
source is newer, and stops with a reason if there is no build at all to open. Headless tests: 33 in
Vitest (the rig table, the reactions, the speech queue with a delayed start and barge-in, the
dispatcher, the chip colours, push-to-talk). Added 2026-09-12: `expression.test.ts` (the
expression settles at the end of the turn, the next tag cancels a pending release — ADR-020
amendment) and `audio_only.test.ts` (the audio-only fallback when TalkingHead or the GLB cannot
load: sentences still play in order and reach the chat as their audio starts). **Gate M3c (the
live 10-turn acceptance) is not met.**

### 10. Latency instrumentation — cross-cutting — **M2 onward, enforced at M3**

**Contract:** every turn logs a structured stage breakdown; rolling p50/p90 tracked per session;
the same record is sent to the client as `timing`.

- **Test** — The timing record is emitted for every turn including barged-in and errored ones;
  the Claude stage uses `result.ttft_ms` / `duration_api_ms` as ground truth; when fillers exist,
  filler-masked turns log **true first-content latency separately** from perceived latency.
  **Fillers are not built — deferred to the end of the project** (ADR-008 amendment, 2026-09-12;
  there is no `FILLER_AFTER_MS`). The voice→voice number is `first_play_ms` — playback start; for
  the browser, the hand-over to the page, the WS hop not included — and `first_audio_ms` is when
  synthesis finished; until 2026-09-12 the latter was stamped as if it were the former
  (`voice_loop.TurnTiming`).
- **Validate** — A scripted 20-turn conversation produces the p50/p90 report.
- **Integrate** — the console `timing_line` / `/profile` command, the page's `timing` message,
  and the session JSONL (there is no `--profile` flag).
- **Gate M3d (hard)** — **voice→voice p90 ≤ 5.0 s over 20 turns** (3.0 s until 2026-09-10, ADR-033). Fillers do not count toward
  meeting it. Missing this gate blocks M4.
- **Harness: built 2026-09-10** — `python -m backend.tools.latency_run` (recorded clips → warm
  Whisper → scripted line to a real session → first complete sentence → its synthesis, + 0.15 s
  slack; opening turn reported separately). Still missing from the contract: the live `timing`
  message, rolling p50/p90 in the session log, and ttft/thinking on voice turns.
- **Baseline 2026-09-10 — FAIL** (over the 3.0 s gate of the time, and 0.30 s over the 5.0 s gate
  the user set the same day, ADR-033). sonnet, effort medium, tools on, 20 turns, app running alongside
  (shared GPU): voice→voice **p50 3.49 s, p90 5.30 s**; stt p50/p90 0.51/0.61 s; Claude first
  sentence **2.61/3.99 s**; tts 0.30/0.61 s; opening 3.98 s. The Claude stage tracks the model's
  **thinking**: thinking p50 306 chars; the five turns with zero thinking ran 1.70–2.56 s
  voice→voice, and the 730-char turn 8.29 s — roughly +0.5 s per 100 chars. `--effort medium`
  does not keep thinking off (spec §10 requires it OFF). Rows: logs/latency/20260910-171340.jsonl.
- **Clean re-run 2026-09-10 (app stopped, GPU idle) — FAIL against 5.0 s:** voice→voice
  **p50 3.89 s, p90 6.03 s** — worse than the shared-GPU baseline, so sharing was NOT the cause.
  STT stayed at 0.51/0.58 s on its own (large-v3, beam 5, 2–4 s clips): over its 0.35 s budget by
  itself. The Claude stage is ttft-bound and noisy run to run: less thinking than the baseline
  (p50 179 vs 306 chars) yet a higher ttft (p50 2.71 s), and one turn with NO thinking waited
  11.4 s for its first token — server-side variance, not ours. **Twenty turns are too few to judge
  a change against a p90 this noisy:** compare settings over repeated or longer runs.
  Rows: logs/latency/20260910-172211.jsonl.

### 11. VRAM instrumentation — cross-cutting — **M2 onward**

- **Validate** — `make doctor` and `orchestrator.watch_vram` read `nvidia-smi`; a warning fires
  above `VRAM_WARN_GB` (10 GB); the `stt` chip shows the model's share.
- **Gate M3e (hard)** — Steady-state total ≤ 10 GB with the browser open and a conversation
  running. Over cap → fall back to Whisper `medium` int8 per ADR-004 and re-measure.

### 12. WaniKani fetcher — `backend/srs/http.py` (M0) + `backend/srs/wanikani.py` — **M1**

Built **before** the Claude session so the read-only guarantee is proven first and the M1 REPL
runs with a real profile. `srs/http.py` is the GET-only client every SRS module is built on: it
has no setter and no write method, and a test asserts that. Adding one requires a new ADR
superseding ADR-021 — which the user has said will not happen.

- **Test** — Against the V0.5 fixtures: level, per-stage counts, ~30 recent unlocks (kanji +
  reading + meaning), ~15 leeches derived by low stage + high incorrect count. Disk cache under
  `.cache/` honours `SRS_CACHE_TTL_S` when set (a younger snapshot is not re-fetched at launch); at the default 0 every launch fetches;
  `control: resync` bypasses the TTL but not the rate limiter. Rate limiting stays under
  ~60 req/min. Status transitions: no token → `disabled`; fetch ok → `ok`; fetch fails with
  cache → `stale`; fetch fails without cache → `error`. The token never appears in a status
  `detail` or `last_error`. **Read-only (ADR-021):** the fetcher is built on
  `backend/srs/http.py`, which has a `get()` method and nothing else — a test asserts no
  `post`/`put`/`patch`/`delete` attribute exists; a recording transport asserts every request in
  a full fetch is `GET` — the standing version over a whole mocked session (launch fetches,
  `resync`, all three Bunpro MCP tools) is `backend/tests/test_srs_readonly_session.py`
  (2026-09-12). Pagination follows `pages.next_url` up to `MAX_PAGES` (20) and a capped
  collection is reported in `last_error`; a 429 ends the fetch with no retry and reads `stale`
  "rate limited"; a `partial` snapshot is never re-used (`test_srs_fetchers.py`,
  `test_srs_http.py`). (V0.9: `/v2/user` does not expose scopes, so there is no
  write-capable-token fixture and nothing for the doctor to warn on.)
- **Validate** — One live fetch with a real token, inside the 10 s session-start budget. The
  token's scopes cannot be read back (V0.9): `make doctor` prints the ones to leave unticked and
  `--live` proves the token authenticates with one GET.
- **Gate M1c** — Absent token, expired token, and network failure each produce a clean session
  with the correct status (chip from M3; console/log status from M1). Read-only recording test
  green.

### 13. Bunpro fetcher (M1) + MCP (M4) — `backend/srs/bunpro.py`, `backend/srs/bunpro_mcp.py` (if written), `mcp.json`

Treated as fragile by design (ADR-010). The fetcher lands in **M1** on the GET-only client,
alongside WaniKani. The MCP server is whatever V0.7 decided and lands in **M4**.

- **Test** — Every call path wrapped: malformed response, auth failure, timeout, and total
  absence each log a warning and continue. `mcp.json` is generated by `tools/mcp_config.py`
  from the resolved config into `.cache/` (no template), and a test asserts no credential is ever
  written to a tracked path — since ADR-024 the Bunpro entry's `env` block carries only the
  snapshot path, no token at all. If we wrote the MCP server:
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
  and rendered into `.cache/prompts/<session-id>.txt` (one file per brain).
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
  session that triggers an MCP tool error and a WaniKani auth error; assert no configured secret
  value and no `Bearer …` string appears under `logs/` or `.cache/` except `.cache/mcp.json`.
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

**Status 2026-09-11 — the face half is built; the rate has a meter.** Every tag the voice table
knows has a rig (`test_models.py` checks both tables agree), and the face changes with the audio,
not before it (subsystem 9). The rig panel's *say it* row plays each emotion's sample sentence for
the scripted check. `python -m backend.tools.emotion_rate` reads the turn logs and prints the
share of turns carrying a tag against the 30 % gate. **Not yet run** on a normal conversation.

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

**Status 2026-09-10 — the rotation half is built, hermetically tested, and OFF by default.**
`backend/session.py` (`Rotator`): arms at `CONTEXT_ROTATE_AT` × the model's window (read from
`brain.meters` after every turn — fields verified, constants.py), starts the replacement in the
background with a handoff from the lesson's turn log (`prompt.build(handoff=...)`, own 600-token
budget, "carry on, do not greet again"), swaps only at a turn boundary via `VoiceLoop.before_turn`,
closes the old session only after the new one has taken a turn, discards a pending replacement on
a tutor switch, and gives up after 3 failed starts (the provider's own compaction then being the
logged fallback). `tests/test_session.py` covers the contract above. **2026-09-12:** the handoff keeps the
**newest** lines within `HANDOFF_MAX_TOKENS` (`prompt.build(keep="tail")`), covers only this
launch's turns (`memory.excerpt_of(..., since=launch_stamp)`), and is omitted entirely before the
launch's first turn so a replacement spawned then greets normally — the first lesson of the day is
greeted (user directive; ADR-032 amendment). The heading is `prompts/handoff.md`.

**Status 2026-09-11 — on by default at 0.7, and adaptive.** V0.12 closed without a one-off
measurement (V0 table): the CLI announces every compaction, so the brain emits `Compacting` events,
the page says she is tidying her notes, the turn records `compaction_ms`, and an `auto` compaction
that arrives before our threshold lowers it to 85 % of where it happened (floor 10 %, persisted in
`.cache/compaction.json`, never switching rotation on from 0). The per-turn timeout restarts when a
compaction starts and when it ends, so a long one is not cut off as a stuck turn. Tests:
`test_session.py` (learning), `test_brain_claude_cli.py` (the real event shapes),
`test_voice_loop.py` (recorded on the turn). Gate M4c (a 40-minute live lesson crossing a
rotation) is **not met**.

**Status 2026-09-10 — the memory half is built, pulled forward from M4 by user directive.**
`backend/memory.py`: the turn log (§6b schema, append-only, recorded from `on_turn` after
`TurnComplete`), the start-of-session read into a `{{memory}}` prompt section, the summariser (run
at the next launch on `MEMORY_SUMMARY_MODEL`, driven through an injected `ask`), and a fourth tier
the original design lacked — `topics.jsonl`, rendered as *recently discussed — do not open on
these*. Hermetic tests in `test_memory.py` cover the schema, append-only writes, a half-written
last line, empty-renders-to-nothing, topic recency and de-duplication, a malformed summary leaving
the previous memory intact, a failing summariser costing recall rather than the lesson, and the
worst-case prompt budget with memory maxed. **Not yet done:** the critical-path phase-recording
test described above, and rotation (ADR-032), which stays last by design. **Not validated live
yet:** close and reopen after a real lesson and confirm she opens from the brief without repeating
the topic.

**Status 2026-09-12 — a fifth tier, and one drawer per tutor** (user: "give the teacher some
memory… remember if we talked about her pet already, what is my name, which country I live in",
then "memory per persona"). Two short hand-editable lists: `about-me.md` — what is durably true of
the student, shared by the whole cast — and `<tutor>/facts.md`, what THAT tutor has claimed about
their own life, so a switch of voice is a switch of person and not a tutor with two cats. Caps 10
and 6, one line each, deduped on the letters; over the cap the oldest survive with the newest
three always given a slot, so a name learned in lesson one is not pushed out by a month of cake.
The brief and the topics moved under `<state>/memory/<tutor>/` too, the turn log records the
persona so only its own tutor summarises a lesson, a pre-persona memory migrates to whoever is
teaching when it is first read, and a live tutor switch moves the memory in place. The summariser
prompt asks for both fact lists and writes the tutor's own without pronouns — the tutor is a man
or a woman depending on the chosen voice. Cost: `MEMORY_MAX_TOKENS` 220 → 400, total 2950 → 3150.
Tests: seven new ones in `test_memory.py` (both sides render, one fact however worded, the first
facts survive a full list, the files are editable and their comments stay out of the prompt, a
summary with no facts leaves disk alone, each tutor's own drawer, a lesson summarised by the tutor
who taught it, the migration). **Not validated live** — the first real check is two lessons with
the same tutor, then switching.

**Known risk.** Rotation is the only feature in this milestone that can break a working
conversation, and its failure mode is subtle — a tutor that quietly forgets. Ship the log, the
start-of-session read and the summariser first; rotation is on by default at 0.7 of the
window (140k tokens on a 200k window, well past an ordinary lesson), so it first fires only in a
very long one. `CONTEXT_ROTATE_AT` unset means "never rotate", which must remain a supported
configuration.

### 19. Settings store & interface — `backend/config.py` + `frontend/src/settings.ts` — **M0 (store) / M3 (drawer) / M5 (full page)**

**Contract:** one schema in `config.py` drives configuration and the settings page — it is the
whole key inventory. Resolution is defaults → `settings.json` → `ATAMA_*`. There is no `.env`
(ADR-022 amendment, 2026-09-12). Secrets never return to the browser in full (ADR-022).

- **Test** — Resolution order (a key set in all three sources resolves to env; in two, to
  `settings.json`; in none, to the default). `settings.json` is written atomically and with mode
  `0600` where the OS has modes (the mode assertion is skipped on Windows, where the profile's
  ACLs are the protection; the atomic rename is asserted everywhere). `config.load()` rejects a
  value outside its choices or range with a one-line `ConfigError` (`test_config.py`). A
  `settings` WS update with an invalid type is rejected with `error` and nothing is persisted.
  The echo of a secret is `{set: true, hint: "…" + last 4}` and never the value. Inventory test:
  every key in `config.py`'s schema is presentable on the page (`.env.example` is gone, and the
  test that compared the two with it). First-run test: with no secrets set the initial `settings`
  echo lets the page show the first-run card. There is no `settings_test` message; "does it work"
  is the status chips and `make doctor`.
- **Validate** — Fresh clone, **no `.env`**: start the app, enter tokens on the settings page,
  watch the chips go green after Refresh, hold a conversation. Change `model` and confirm the
  conversation memory survives the respawn. Restart the app and confirm everything persisted.
- **Integrate** — The `settings` message (subsystem 7) and the status registry (subsystem 16).
- **Persona picker (user directive 2026-09-10).** `TUTOR_PERSONA` is one setting that switches
  character, voice *and* face together (ADR-026/030), so the settings page offers it as a single
  choice rather than three. Two rules the picker has to respect:
  *only offer personas whose declared avatar actually exists* — `prompt.declared_avatar()` names
  the file and `check_avatar` says whether it is usable — and *show why one is unavailable*
  rather than hiding it, because "たなか needs an avatar" is actionable and a missing row is not.
  Only `minami` ships with a face today; the rest wait on commissioned models.
- **Gate M5b** — The M5 acceptance: clone → first conversation without ever creating a `.env`.

---

### 20. Second brain provider — OpenAI, headless — `backend/brain/openai_cli.py` — **deferred, user directive 2026-09-10**

ADR-027 made the brain a provider behind an interface precisely so this is a new module rather
than a rewrite: `BRAIN_PROVIDER` already exists as a config key and already says
"Implemented: claude-cli". Nothing upstream of the chunker imports a provider.

**This does NOT reopen ADR-001.** That decision forbids *substituting* the Anthropic API for the
`claude` CLI — subscription auth, no per-token billing. A second provider sitting beside it is
the case ADR-027 was written for. Adding OpenAI must not change how the Claude provider is
spawned or authenticated.

**Decided 2026-09-10 (user):** the **`codex` CLI driven headless as a subprocess, on a
subscription** — the same shape as the Claude provider, not the HTTP API. This preserves ADR-001's
actual reasoning (subscription auth, no per-token billing) rather than routing around it, so no
ADR is superseded and none is needed: this is ADR-027's anticipated second implementation.

Consequences that follow from picking the CLI:

- **A verification spike comes first, like V0.1/V0.2 did for Claude.** The exact flags for
  headless streaming, the event shapes on stdout, how a session id is assigned and resumed, and
  how to prove the process is on subscription auth rather than an API key — all pinned live in
  `constants.py` with a date. **Do not write a line of the provider against remembered flags**
  (ADR-015). The Claude spike found `--tools ""` behaviour, an MCP readiness race and a
  system-prompt truncation bug that no amount of reading would have predicted; assume the same
  density of surprises here.
- **The spawn hygiene of §4/ADR-016 applies to any brain subprocess, not just Claude:** empty
  cwd outside the repo, allowlisted environment, no inherited MCP config, and an assertion at
  init that the auth source is the subscription and not a key in the environment.
- **Test** — The provider contract test runs against BOTH implementations: the same `Brain`
  interface, the same event types (`TextDelta`, `Thinking`, `ToolCall`, `ToolOutcome`,
  `RateLimited`, `TurnComplete`), the same restart/resume behaviour. A provider that cannot
  produce the full event set is a provider the pipeline cannot use.
- **Do not fabricate the interface** (ADR-015). Whichever reading wins, its flags or endpoint
  shapes get verified live and pinned in `constants.py` with a date, like every other external
  interface here.
- **Sequencing:** after M3. The brain already works; a second one buys optionality, not a
  milestone, and M2/M3 are not blocked by it.

---

### 21. `make doctor` — `backend/tools/doctor.py` — **M0 deliverable; found missing 2026-09-10, WRITTEN 2026-09-12**

The Makefile target existed and invoked `backend.tools.doctor` while the module did not, so
`make doctor` had never run despite M0 being recorded as done. It exists now
(`make doctor`, or `.venv/Scripts/python -m backend.tools.doctor`; `--skip-claude` skips the
`claude -p` probe, `--live` adds ONE GET per configured SRS token — never by default, ADR-024).
Every check prints one PASS / WARN / FAIL line with what to do; the exit code is non-zero on any
FAIL; nothing prints a secret. **Checks, in order:** `claude` on PATH and its version against the
pin; `ANTHROPIC_API_KEY` absent (FAIL if present); the `claude -p "respond with OK"` probe under
the app's own spawn rules (allowlisted env, cwd outside the repo, no shell) asserting
`init.apiKeySource == "none"`; VOICEVOX, the app port and SearXNG loopback-only (FAIL if any
answers on a LAN address); the Docker engine and the compose containers; the tokens as set/unset
with their hints; the WaniKani scope reminder (cannot verify — V0.9); with `--live`, one GET per
token; `.gitignore` via `git check-ignore` on every personal/generated path; the pre-commit hook;
CUDA via `nvidia-smi` against `VRAM_WARN_GB`; the persona's avatar file; the §15 topology (native
Windows / WSL2 / Linux, with the WSL2 `/mnt` warning); then the last persisted §5b status table.
Hermetic tests in `test_doctor.py` cover every branch through injected effects. The M0 acceptance
line "actionable errors for every missing prerequisite" is **met** by this; the M0 gate as a whole
(M0a) was already green. Not on the list yet, from the paragraph below: the audio-device check.

Spec M0 defined what it must check: `claude` CLI present and version-pinned, `ANTHROPIC_API_KEY`
absent from the shell, a trivial `claude -p` returning `init.apiKeySource == "none"`, CUDA
visible, VOICEVOX reachable on loopback **and not on other interfaces**, tokens present, every
ignored path actually ignored via `git check-ignore`, and the §5b status table printed.

Add to that list, from what later milestones turned up:

- **the avatar** — `frontend/public/avatar.glb` present and passing `check_avatar`; a fresh clone
  has no face and nothing currently tells the user that until the frontend silently fails.
- **audio devices** — at least one input and one output on a host API that can actually be opened
  at the fixed rates (see `CAPTURE_HOSTAPIS`); on Windows a device may enumerate and still be
  unopenable, which cost a long debugging session.

### 22. Study panel — `backend/annotate.py` (planned) + `frontend/src/chat.ts` — **M3 extension, user request 2026-09-11 (ADR-036, spec §8b)**

**Contract:** the conversation as a chat thread beside the avatar; her grammar uses in red and
clickable, words clickable for readings and meaning, a translate icon per sentence, a hint for
what she wants the student to use — with no model call unless the student clicks.

- **Verify first (ADR-015).** The tokenizer and dictionary packages (candidates: fugashi +
  unidic-lite; a JMdict / KANJIDIC2 source) — install, read their real API, pin versions and a
  dated finding in `constants.py`. Measure their load time and memory at startup.
- **Test** — Chunker: `{{span|point}}` and `[target:point]` are stripped from the TTS text and the
  subtitle text in every position (mid-sentence, across a chunk boundary, malformed, nested), and
  their spans land on the right characters of the cleaned sentence. Annotator: words and readings
  for a fixed sentence (golden), WaniKani data preferred over the dictionary, an unknown word
  degrades to no card. Explain: one call per uncached request, none for a cached one; a failed
  call returns an error message, never a stuck spinner. Page: bubbles, spans and cards render from
  a recorded `speak`; the hint stays closed until clicked.
- **Validate** — A normal 10-turn lesson: grammar tagged in most turns she teaches something (count
  with the turn log, like M3f); every red span is the grammar it names; a click shows the rule in
  the chosen language within a few seconds, the second click instantly.
- **Gate** — To be set with the user when it is built. Latency must not move (ADR-033): tags are
  output tokens, and a turn with them is measured like any other.

**Status 2026-09-12 — the chat, the marks and the hint are built; explanations, translation and
word cards are not.** The tutor's rules are in `prompts/tutor.md` (TOTAL_MAX_TOKENS 2350 → 2550);
`chunker.py` strips `{{span|point}}` and `[target:…]` in every position, split across deltas or
malformed, and records the spans (`test_chunker.py`); they reach the page as `speak.grammar` and
`speak.target` and the turn log records them. The page (`frontend/src/chat.ts`, `STUDY_PANEL`)
puts the tutor on the left and the conversation on the right, her grammar in red with a card on
click, and a lightbulb that lights when she asks for a form and clears when the student answers.
Her sentence joins the chat when its audio starts; one whose audio never started (TalkingHead drops
what it cannot play) still joins it at the end of her turn. Checked in real-time headless Chrome
against a demo orchestrator. **Not validated live**, and the tag rate is not measured yet.

**Status 2026-09-12 — furigana is built.** `backend/annotate.py`: fugashi + unidic-lite (pinned,
verified in constants.py) give a reading per run of kanji — 取り消す as 取=と, 消=け — corrected from
`backend/data/readings.txt` for what the dictionary gets wrong in lesson Japanese (私, 日本, 明日,
何を). `known` comes from WaniKani: the fetcher also reads kanji assignments and the kanji subjects
up to the student's level (GET, launch and Refresh only, ADR-024) and counts a kanji as known when
`passed_at` is set — there is no `passed` filter in the API (docs read 2026-09-12). The readings
ride on `speak.readings` and `stt_final.readings`; the page renders `<ruby>`, and the `FURIGANA`
setting (all / unknown / off, default **unknown**) re-reads the conversation live. Tests:
`test_annotate.py`, `test_wanikani_kanji.py`, `chat.test.ts`. **Not validated live**; a student with
no WaniKani data sees furigana on everything, which is the intended fallback.

**Status 2026-09-12 — what the student got right floats behind her** (user). `[used:word]` in her
reply, stripped like the other marks, reaches the page as `speak.used`; the page floats it up the
mood layer in gold and logs it. Free: one more tag, no call. Also this day, because the launch sat
for 80 s summarising five old sessions and looked hung: the launch now summarises only the newest
session (the one she greets with), the rest are caught up by a background task once the student is
talking, each reported as it goes, and an older catch-up can no longer overwrite a newer brief
(`test_memory.py`). The prompt's ceiling went 2550 → 2650 for the new rule.

**Status 2026-09-12 — explanations and translations on click are built.** `backend/explain.py`:
a click sends `explain`, one worker (`claude -p`, haiku tier, no tools, §4 rules, never the tutor's
session) answers, and every answer is cached on disk under `.cache/explain/` per grammar point and
language, and per sentence — the second click is free. `EXPLAIN_LANGUAGE` (en | ja) chooses the
language of a grammar explanation; a sentence translation is always English. The page shows the
rule in the grammar card and the English under the sentence, from the 訳 button on each of her
bubbles. Failures come back as a message, never a spinner. Tests: `test_explain.py` (asked once,
cached, language in the key, empty answers not cached, cache survives a restart) and the protocol
samples. Checked in real-time headless Chrome. **Not validated live.** Chat polish the same day
(user): one bubble per sentence, Noto Sans JP with the OS fallbacks, and the panel restyled to the
console-home look.

**Status 2026-09-12 (late) — six notes from the user after looking at the page, all done.**
1. *Red is grammar, not vocabulary.* A noun had gone red and 〜てみよう had not been marked at all.
   `prompts/tutor.md` now defines a grammar point, shows the whole inflected form being wrapped
   ({{食べてみよう|〜てみる}}), and lists the forms it was forgetting (volitional, 〜てみる, 〜ておく,
   〜てしまう, 〜ている, potential, passive, causative, 〜たい, 〜すぎる, 〜たことがある, 〜なければならない,
   〜ながら, 〜ので, 〜たら/〜ば/〜なら, 〜ても, 〜と思う) — and says a noun, a plain verb, an adverb, a name
   or a number is never a point. Belt and braces: `Annotator.grammar_only` drops a mark whose every
   token is a noun unless the point is named as a pattern (〜…), using the tokenizer already loaded
   for furigana (`test_annotate.py`, four cases).
2. *Produce, not only hear.* She now asks the student to use a named point **every second or third
   turn**, and returns to a dodged one a turn later instead of dropping it.
3. *Her new words.* The profile's "Recent WaniKani vocab (prefer these)" list is now a per-turn
   instruction: one or two of those exact words in every turn, chosen over the synonym she would
   otherwise use. Cost of 1–3: TOTAL_MAX_TOKENS 2650 → 2950, the template ~1771 tokens.
4. *A cancel key.* **ALT GR** during a hold drops what is being recorded: `VoiceLoop.ptt_cancel()`
   empties the buffer and closes the hold, so releasing SPACE afterwards sends nothing and the next
   press starts clean (`control: cancel`, `test_voice_loop.py`, `mic.test.ts`). The right-hand ALT
   because Chrome claims SPACE with the left one. The spec had predicted this key (§9b); it exists
   now. The status line under the talk button says so instead of repeating "hold SPACE".
5. *The settings cog moved into the status pill*, at its right end: floating over the scene it was
   in the way of both the avatar and the chat.
6. *The chat.* It stopped a line short of the bottom — `scroll-behavior:smooth` fired scroll events
   on the way down, which read as the student scrolling away, and furigana, the webfont and a
   translation each grow a bubble after it was scrolled to. It now jumps now, next frame, and once
   the fonts settle, keeps following unless the student scrolls up, and ignores its own scroll
   events. Wider (680px / 48vw) and bigger (20px). Checked in real-time headless Chrome:
   14 bubbles, the last flush with the bottom, 404px of scrollback above it, ALT GR reaching the
   server as `cancel` and the release sending nothing.

**Measured the same day — Haiku is not slow, the first call is.** The memory summariser (haiku,
one `claude -p` worker reused for every pending log) took 24.6 s and 39.6 s for its first summary
in two runs, then 3.7 s and 5.9 s for the next on the same worker: the cost is the CLI's first
request, not the model's generation. It is already off the critical path — the launch summarises
the newest session and the rest catch up in the background.

**Status 2026-09-12 (from the first real launches) — four findings, all fixed.**
1. *She greeted the student by a name nobody had given her* (「マリアンさん」). Not from the memory
   files, not from the profile, and not in any earlier transcript — the first occurrence anywhere
   is that greeting. Probed the subprocess with the tutor's own isolation: asked what account the
   session is logged in as, the CLI answers the user's e-mail address, and the model read a name
   out of it. Nothing of ours leaked; it is the CLI's own auth context, which §4's allowlist and
   empty cwd cannot strip. `prompts/tutor.md` now forbids inventing a name, country, job or family
   that the memory section does not carry, and says an e-mail or account name is not an
   introduction. She also invented a cat: new things about herself must now be offered as news,
   not as shared history, and they land in her `facts.md` for next time.
2. *"0 summarised", every launch.* Three of the five pending logs were launches where the student
   never spoke: they can never produce a summary, so they were retried at every start, one model
   call each and the CLI's 25-40 s cold start on the first. A log with no student turn is now
   marked done without a call, and an answer of "there is nothing here" — or one that is not JSON
   — is not asked again; only a failed CALL stays pending. Pending went 5 → 0 on this machine.
3. *The line under the dock was green.* It is white now, like the dock (user: "green is jarring").
4. *The launch waited a minute on the summary, alone.* The same summary took 13.7 s and 52.6 s on
   two runs at the same effort, so its latency is variance, not work — lowering the effort made it
   slower, not faster. The fix the user asked for is ordering, not a smaller model: the summary now
   starts at the top of `run()` and runs while SRS, the page, VOICEVOX and Whisper come up, and is
   awaited just before her prompt is built and her session spawned (so the brain is now created
   LAST). She still never greets having forgotten yesterday, and the launch is one wait instead of
   two. The microphone checklist is one line now, also by request.

**Status 2026-09-12 — the page's stop button is a full stop** (user). `control: quit` sets
`Hub.quit_requested`; both of the REPL's shutdown paths then run `backend.tools.down` after the
subprocess, the speech queue and the server have closed, so VOICEVOX and SearXNG go down with
everything else. Ctrl+C still leaves them up — that is the "back in a minute" exit, and the
containers are slow to start. Test in `test_app.py`: nothing happens unless the button was pressed.

**Status 2026-09-12 (evening) — the wait is visible, and New topic asks first** (user). The launch
prints what it is waiting for and caps the wait at `MEMORY_WAIT_S` (45 s) — past that she starts and
the summary lands for the next launch, because a tutor that never appears is worse than one a
lesson behind; the task is cancelled if the lesson ends first. The page shows one bubble with three
breathing dots and a caption driven by the service chips (`reading back your last lesson…`,
`loading speech recognition…`, `warming her voice…`), removed by her first sentence. The New topic
button opens a confirm card instead of firing on a misclick. Checked in headless Chrome: the bubble
is up with three dots while loading and gone after she speaks; the card sends nothing on "Keep
going" and `control: new_topic` on "New topic".

**Status 2026-09-12 (measured, not guessed) — why the blue was rare.** The user said her words and
the floating word were both too rare, so it was counted over every session of the day: 301 of her
sentences, **13 %** carrying one of the student's not-yet-Guru'd words, 12 grammar marks in all, 9
`[used:]` credits over 62 student turns. Three causes, all fixed: (1) only the newest 30 unlocks
were fetched, so her palette was 21 words — the fetcher now asks for every vocabulary assignment
below Guru (one more id list on the same GET, launch and Refresh only, ADR-024), giving 58;
(2) matching was exact, so 「気に入ります」, 「勉強しました」 and a kana 「たけのこ」 all missed — a word now
matches as written, as its kana reading, and by the prefix every inflection shares; (3) the float
waited for her `[used:]` tag, so the page now floats one of the student's own words the moment the
transcript arrives, her credit still floating separately (and it is the only one that can be a
grammar point). Re-measured on the SAME transcripts: 13 % → **21 %** of her sentences, 65 % of her
turns. Also fixed: the loading bubble came back mid-lesson saying "she is thinking of how to
start", because the §5b status heartbeat re-sends `brain: ready` every few seconds — it is now
shown only before her first sentence.

**Status 2026-09-12 — every unlearned form, not two ghosts** (user: "she must use all the forms I
haven't Guru'd or mastered, with a preference for the all new fresh stuff"). The Bunpro fetcher
reads the beginner, adept and seasoned levels beside the ghosts (three more GETs, launch and
Refresh only, ADR-024): 2 + 10 points became **68 in play**, ordered weakest first. The profile
renders them in that order — ten with meanings, the rest by title — and `prompts/tutor.md` says to
go through the list rather than orbit the top of it. `backend/study.py` matches all 68 for the red
marks' verification and the float's colour. Cost: `TOTAL_MAX_TOKENS` 3250 → 3400, and the template
is now ~1985 tokens: **the next session's first job is to rewrite these rules shorter**, not to
raise the ceiling again (item 6 below).

## Next session — what is left (set 2026-09-12; items 3 and 4 were done the same day)

**1. One real lesson on the new page, first.** Refresh study data at the start (Settings → Account)
so the kanji progress is current. Watch: grammar in red, the hint lighting when she asks for a form,
the floating word when the student gets one right, furigana only on kanji not yet passed. If the
tutor switch fails again, the `[tutor]` lines in the terminal are the evidence that is still missing.

**2. Measure what that lesson shows.** `python -m backend.tools.emotion_rate` for the emotion tags,
plus the same measure for the study marks — how often she tags grammar, sets a target, credits the
student. A low rate is prompt wording, not plumbing (ROADMAP 17, gate M3f).

**3. Explanations on click (ADR-036 point 2).** DONE 2026-09-12 (status note above).

**4. Translate icon per sentence.** DONE 2026-09-12 on her sentences. Still to decide: whether the
student's own sentences get one too.

**5. Word cards (ADR-036 point 3).** HALF DONE 2026-09-12: a blue word opens a card with its kana
reading, English meaning and WaniKani stage, all of which ride with the sentence — no dictionary,
no round trip. What is left needs one: on'yomi/kun'yomi per kanji, and words outside their WaniKani
items. JMdict_e.gz (10.6 MB) and kanjidic2.xml.gz (1.5 MB) from EDRDG are verified reachable and
are CC BY-SA; download at setup into `.cache/`, never committed, credited in the README.

**6. Prompt size.** 2950 tokens after the three teaching notes of 2026-09-12 and rising with every
rule (ADR-011, §10). Re-measure a turn and tighten the wording rather than let it drift — the
elicitation, marking and vocabulary rules now say some of the same things twice.

**7. Latency re-measure on Sonnet 5.** The 5.0 s gate (M3d, ADR-033) was last measured on
Sonnet 4.6. Run the 20-turn harness once there is a real lesson to measure.

**Left for a deliberate session:** M3b (10 interruptions on headphones under 300 ms; 10 turns on
speakers with zero self-interruptions), M3c (the 10-turn acceptance), M3e (VRAM ≤ 10 GB).

**Status 2026-09-12 — `.env` is gone, and an empty install explains itself** (user). `config.load()`
no longer reads it: defaults → `settings.json` → `ATAMA_*`, and `config.py`'s schema is the only
inventory, so `.env.example` and the test that kept the two in step are deleted (a test now asserts
every key is presentable in the panel). `migrate_env` imports an old file in one command and moves
the tokens too — nothing reads them where they were. `config.stale_dotenv()` reports leftovers and
the launch prints the fix. The panel's `pinned` now means the environment only. Onboarding: with no
keys the launch says what is missing, where to add it, what happens meanwhile and how to be offline
on purpose (`--no-srs`); the page shows a first-run card built from the schema's unset secrets —
naming no service, because the read-only gate forbids it — with one button to Account and one that
dismisses it for good. Tests: `test_config.py` (a leftover `.env` configures nothing, the bootstrap
key is not reported), `test_settings_view.py` (a leftover `.env` pins nothing), `test_migrate_env.py`
(the tokens move, the configuration comes out the same without the file).

**Status 2026-09-12 (later) — blue is their own vocabulary, and the launch needs no file at all.**
`backend/study.py`: the words the student has not yet Guru'd (WaniKani stage < 5, from the snapshot
already fetched — no new call, ADR-024) and the grammar they have not mastered (Bunpro ghosts and
beginner points). Her sentences and their own carry `vocab` spans, blue in the chat, longest match
first, never overlapping a red grammar mark — grammar wins where they meet. The float behind her is
checked against the same lists: `used_kind` says which list it came from, and the word drifts up
blue for a word, red for a grammar point, gold when the tutor credits something from neither.
Tests: `test_study.py` (only what is not Guru'd, longest match, one-character words skipped, what
the tutor credits is verified, no SRS data is no colour) and three in `chat.test.ts`. Verified in
headless Chrome: seven blue words, and the float arriving as `kao word vocab`.
Also: `SEARXNG_SECRET` is generated by the launcher into the per-user state directory instead of
living in `.env`, because a fresh clone had nothing to interpolate and `docker compose up` failed
before anything started (user). There is now no `.env` in the repo at all.

**Status 2026-09-12 (night) — five fixes from review, and the orchestrator split.** The Claude
subprocess is interrupted with the CLI's own `control_request`/`interrupt` on stdin, never a
signal (ADR-037; `taskkill /T` for the whole tree on Windows, `--resume` silently if an interrupt
never settles; `init.session_id` asserted on resume too). `first_play_ms` is the voice→voice
number; `first_audio_ms` is synthesis done. The WS handshake checks `Origin` (ADR-017 amendment);
each page has its own outbox with `mic_level` coalesced, a stuck page is closed, a page that
drops mid-hold has its hold cancelled, and `blur` cancels a capture. The expression settles at the
end of the turn (ADR-020 amendment); the avatar falls back to audio-only playback with a notice.
The handoff is the newest lines of this launch only and is omitted before the first turn, so a
switched-in tutor greets (ADR-032 amendment); the turn log is one file per lesson; `tools`/`usage`
are filled on the voice path. `ReadOnlyTransport` is the runtime guard's name (`_GuardTransport`
kept as the alias the gate pins); a refusal is CRITICAL and the chip reads `error`; WaniKani
pagination to 20 pages with truncation reported; 429 → `stale` with no retry; `partial` snapshots
re-fetched. `make doctor` exists (subsystem 21); `make check-secrets` flags a leftover `.env` and
the WaniKani/Bunpro token shapes; `config.load()` validates choices and ranges; `app.py` pre-binds
the port and names `PORT` if it is taken; `run.cmd` says how to create `.venv` when there is none.
The docker images are pinned (`voicevox/voicevox_engine:cpu-0.25.2`,
`searxng/searxng:2026.9.8-3fdc6d753`). **`backend/repl.py` is split** (spec §13): `repl.py` is
the CLI entry only (137 lines: `parse`/`run`/`text_loop`/`main`; flags `--refresh --no-srs
--no-open --speak --listen --browser --show`; there is no `--profile` flag — `/profile` is a
slash command of the text loop beside `/status`, `/prompt`, `/quit`; the banner reads
"atama-AI · voice lesson / text lesson"). `backend/orchestrator.py` holds the `Lesson` class —
SRS sync, page, voice, memory, prompt, brain; `account`, `open_lesson`, `switch_persona`,
`spawn_rotation`, `resync`, `adopt`, `close` — plus `listen()`, `load_stt`, `check_microphone`,
`stop_containers`, `summarise`, `one_turn`, `watch_vram` and `sanitised()`.
`backend/page_control.py` holds `PageControl` (`from_browser` dispatch, `do_resync`,
`change_tutor`, `settings_saved`, status routed through `registry.sanitize`) and `LevelSender`
(`mic_level` coalesced to one send per 0.1 s). `backend/terminal.py` holds the console rendering
(`LevelMeter`, `timing_line`, `session_report` with nearest-rank percentiles). Every tool exits
with one line on a malformed `settings.json` (`ConfigError`). Tests: `backend/tests/test_repl.py`
(12, hermetic). Prompt text moved to files: `prompts/handoff.md`, `prompts/memory.md`,
`prompts/summariser.md` (ADR-012).

## Live checks pending (2026-09-12)

Each of these is code-complete and hermetically tested, and **not yet observed on the real
machine**. None is a gate; every gate's status is unchanged by this list (M2a–M2d per ADR-034,
M3b–M3f, M4c and M4d all remain not met).

1. **Barge-in, then a clean next turn, on Windows** — press the key over her, confirm the
   `control_response` and `error_during_execution` result arrive, the next turn is answered on the
   same session id, and `tasklist` shows **no orphan `claude.exe`** (ADR-037).
2. **A turn timeout, then a clean next turn** — the apology line, then a normal answer.
3. **A persona switch before the first turn greets** — no "carry on" handoff to a student who has
   not spoken (ADR-032 amendment).
4. **A late-booting VOICEVOX switches to the real styles on the first sentence** that reaches it
   (`ensure_table`), rather than staying on the base style for the lesson.
5. **`pauseLengthScale` semantics** — synthesise one sentence with a 、 at scale 1.0 and 2.0, same
   `speedScale`, and compare the WAVs against `VOICEVOX_PAUSE_SCALE_SEMANTICS`; then flip
   `constants.VOICEVOX_PAUSE_SCALE_VERIFIED` to `True` with the date.
6. **Browser first audio right after a PTT barge-in** — the interrupted sentence's late audio is
   dropped (epoch) and the new turn's first sentence plays without a stale one in front of it.

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
   `apiKeySource`, schema-is-presentable inventory, and protocol contract tests.
3. `make check-secrets` — no token-shaped strings (WaniKani and Bunpro shapes included) and no
   current `settings.json` value in the tracked tree; a leftover `.env` is flagged.
4. **20-turn latency run** — p50/p90 reported; p90 ≤ 5.0 s (`python -m backend.tools.latency_run`).
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
