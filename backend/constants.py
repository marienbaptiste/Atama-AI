"""Verified external-interface findings, dated. Re-verify on every upgrade (ADR-015).

Nothing in here is a guess. Each block names what was checked, how, and when.
"""

# --- Claude Code CLI ---------------------------------------------------------
# Verified 2026-09-09 against `claude --help`, Claude Code 2.1.159, plus one live
# stream-json run (see ROADMAP.md "V0 findings log").
CLAUDE_CLI_VERSION_VERIFIED = "2.1.159"

# Base argv for the persistent tutor subprocess (spec §4). Model/fallback/session id/
# prompt/mcp config are appended by backend/brain/claude_cli.py at spawn.
CLAUDE_BASE_ARGS = (
    "claude", "-p",
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--include-partial-messages",   # "only works with --print and --output-format=stream-json"
    "--verbose",
    "--strict-mcp-config",          # "Only use MCP servers from --mcp-config"
    "--tools", "",                  # "Use "" to disable all tools" (built-in set)
)
# init.tools[] observed with the spec's ORIGINAL --disallowedTools list — i.e. what was
# still exposed. This is the V0.8 fallback disallow list if MCP tools do not survive --tools "".
CLAUDE_BUILTIN_TOOLS_OBSERVED = (
    "Task", "AskUserQuestion", "CronCreate", "CronDelete", "CronList", "EnterPlanMode",
    "EnterWorktree", "ExitPlanMode", "ExitWorktree", "Monitor", "NotebookEdit",
    "PushNotification", "RemoteTrigger", "ScheduleWakeup", "Skill", "TaskOutput", "TaskStop",
    "TodoWrite", "ToolSearch", "Workflow",
)
# Under subscription auth the init event reports this. Anything else = API billing (refuse).
CLAUDE_INIT_APIKEYSOURCE_SUBSCRIPTION = "none"
# V0.8 RESULT (live, 2026-09-09): with --tools "" AND the MCP server connected before the first
# turn, init.tools == ["mcp__bunpro__get_ghost_reviews", "mcp__bunpro__get_grammar_progress",
# "mcp__bunpro__get_review_queue"], init.mcp_servers == [{"name":"bunpro","status":"connected"}],
# the tool is called and answered. If the first turn is sent before the server connects, init
# reports status "pending", the model sees NO tools and hallucinates. Claude Code emits no
# "MCP connected" event on stdout, so readiness comes from OUR server's ready marker
# (ATAMA_MCP_READY, written on notifications/initialized ~1.1 s after spawn). The orchestrator
# MUST wait on it before writing the first user turn.
CLAUDE_MCP_TOOL_PREFIX = "mcp__bunpro__"
CLAUDE_MCP_READY_TIMEOUT_S = 20.0
CLAUDE_INIT_TIMEOUT_S = 30.0
# System prompt: ALWAYS use the file variants. `--system-prompt-file` / `--append-system-prompt-file`
# do exist (they are listed only inside the `--bare` help text, not in the main option list).
# Verified 2026-09-09: passing a MULTI-LINE prompt to the string variants truncates it at the
# first newline AND makes the CLI lose every flag that follows, so `--mcp-config` placed after it
# was silently ignored and the tutor ran with no tools. The file variant fixes all of it and keeps
# the student profile out of `ps`.
# `--system-prompt-file` (replace) is the default: with `--append-system-prompt-file` the tutor
# inherits Claude Code's coding-agent prompt and introduces itself as "Claude Code ...
# ソフトウェアエンジニアリングのタスクを支援" (measured), and init.tools came back empty.
# `--effort <low|medium|high|xhigh|max>` is the lever for spec §10's "extended thinking OFF".
# Measured 2026-09-09 over 3 turns (sonnet, both MCP servers loaded), median first-token and
# opening-turn first-token: low 2.75s/8.6s, medium 2.14s/5.8s, high 3.75s/19.2s. Medium is both
# faster AND more considered than low. The FIRST turn of a session is always far slower than the
# rest (prompt-cache creation), which is why the opening turn is measured separately.
# Thinking CANNOT be turned off on Sonnet (docs, read 2026-09-10): code.claude.com/docs/en/
# model-config says Sonnet 5 "always uses adaptive reasoning" and that "the session toggle,
# `alwaysThinkingEnabled`, and `MAX_THINKING_TOKENS=0` have no effect on Sonnet 5" — reasoning
# there is set by --effort. `alwaysThinkingEnabled` (settings-reference) exists for other models;
# `MAX_THINKING_TOKENS` is mentioned but NOT in the env-vars reference, so it is not relied on.
# `claude --help` (2.1.159) exposes only --effort. Measured the same day at medium, 20 turns:
# thinking p50 306 chars and the Claude stage tracks it (~+0.5 s per 100 chars) — see ROADMAP 10.
# Model ids, verified 2026-09-10 (one tiny turn each): the bare aliases LAG — `sonnet` ran
# claude-sonnet-4-6 and `opus` claude-opus-4-8, while claude-sonnet-5 and claude-opus-5 both work;
# `haiku` ran claude-haiku-4-5-20251001. An unavailable id still starts and fails on its first
# turn ("There's an issue with the selected model ..."). Tiers therefore resolve through
# backend/model_tiers.py + backend/data/model_tiers.txt to the newest id the CLI accepts.
# CORRECTION, same day: the `sonnet` alias resolves to **claude-sonnet-4-6** on this account
# (result.modelUsage keys, CLI 2.1.159), not Sonnet 5 — so the Sonnet 5 wording above may not
# describe it. Moot while the user rules out any thinking change (ADR-033); re-verify before one.
#
# Usage fields, verified 2026-09-10 (CLI 2.1.159, one live `-p` stream-json call):
#   result.usage = {input_tokens, cache_creation_input_tokens, cache_read_input_tokens,
#                   output_tokens, iterations: [...per API call...], ...}
#   result.modelUsage = {<model id>: {inputTokens, outputTokens, cacheReadInputTokens,
#                   cacheCreationInputTokens, costUSD, contextWindow: 200000, maxOutputTokens}}
#   result.total_cost_usd — API-equivalent dollars, CUMULATIVE per session (two-turn probe). Not
#                   shown anywhere: through `claude -p` on a subscription it is not a bill (user)
#   rate_limit_event.rate_limit_info = {status: "allowed", resetsAt: <epoch s>,
#                   rateLimitType: "five_hour", overageStatus, overageDisabledReason, isUsingOverage}
# No utilisation percentage and nothing monthly is reported.
# Even with the marker, keep --allowedTools on the three MCP names (ADR-021 belt-and-braces).
# Observed stream types: system/init, system/status ({"status":"requesting"} before each API
# call), rate_limit_event ({"rate_limit_info": {status, resetsAt, rateLimitType: "five_hour",
# overageStatus, isUsingOverage}}), stream_event (Anthropic SSE: message_start,
# content_block_start/delta/stop, message_delta, message_stop), assistant, user (tool_result),
# result/success. Built-in tool names differ by platform/context (e.g. PowerShell on Windows,
# TaskCreate…), so a --disallowedTools list is NOT a reliable fallback; --tools "" is.
# init.memory_paths.auto is keyed by the detected project root — a cwd INSIDE the repo is not
# isolated (CLAUDE.md is discovered up the tree). See config.claude_cwd().
# Spawn WITHOUT a shell: resolve the launcher with shutil.which("claude") (claude.CMD on Windows,
# runnable directly via CreateProcess) and exec it. shell=True + cmd.exe failed with "The current
# directory is invalid." for a cwd under %LOCALAPPDATA% (2026-09-09), and a shell is an injection
# surface we do not need.
# Event types seen on stdout: system/init, rate_limit_event, assistant, result.
CLAUDE_EVENT_INIT = ("system", "init")
CLAUDE_EVENT_RATE_LIMIT = "rate_limit_event"
CLAUDE_EVENT_RESULT = "result"
# Env vars the child is allowed to see (spec §4 allowlist). Everything else is dropped.
CLAUDE_CHILD_ENV_ALLOWLIST = (
    "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMPDIR", "TEMP", "TMP",
    "LANG", "SYSTEMROOT", "COMSPEC", "CLAUDE_CODE_OAUTH_TOKEN",
)

# --- WaniKani (official API v2) ----------------------------------------------
# Origin is a constant on purpose (spec §0 rule 7). Revision header per WaniKani docs.
WANIKANI_ORIGIN = "https://api.wanikani.com"
WANIKANI_REVISION = "20170710"
WANIKANI_MIN_INTERVAL_S = 1.0          # ~60 req/min limit
# Write scopes a token must NOT have. Referenced ONLY by the doctor's warning (spec §0 rule 6).
# (Kept in the doctor module itself, not here, so this file stays scope-string-free.)

# --- Bunpro (unofficial frontend API; ROADMAP V0.7, ADR-023) -----------------
# Origin: what the current read-only community server hardcodes. Confirm live at M1 via
# `make capture-bunpro`; if only bunpro.jp answers, change the constant here (still a constant).
BUNPRO_ORIGIN = "https://api.bunpro.jp"
BUNPRO_API_PREFIX = "/api/frontend"
# Verified live 2026-09-09: without this query parameter the Settings->API "Account API Token"
# is rejected with 401 {"errors":[{"code":"AUTH_USER_DENIED"}]} on every endpoint. With it,
# `Authorization: Token token=<token>` works. Bunpro's own naming — it flags that the token is a
# full-account credential; our read-only transport is the mitigation (spec §0).
BUNPRO_TOKEN_OPT_IN_PARAM = "dangerously_authenticate_using_api_token"
# The frontend API expects browser-like Origin/Referer of the site itself.
BUNPRO_SITE_ORIGIN = "https://bunpro.jp"
# Politeness. 2 s (yash-278) x 7 session-start calls = 12 s > the 10 s budget (found in tests
# 2026-09-09); 1 s matches sawariz0r's <= 1 req/s and keeps the 5-call session start at ~4 s.
BUNPRO_MIN_INTERVAL_S = 1.0
# Read endpoints (all GET). Paths relative to BUNPRO_API_PREFIX.
BUNPRO_READ_ENDPOINTS = {
    "user": "/user",
    "due": "/user/due",
    "queue": "/user/queue",
    "jlpt_progress": "/user_stats/jlpt_progress_mixed",
    "srs_overview": "/user_stats/srs_level_overview",
    "srs_level_details": "/user_stats/srs_level_details",          # ?reviewable_type=Grammar&level=N
    "ghost_level_details": "/user_stats/srs_ghost_level_details",  # ?reviewable_type=Grammar
    "forecast_daily": "/user_stats/forecast_daily",
}

# --- VOICEVOX / TalkingHead ---------------------------------------------------
# TalkingHead verified 2026-09-10 (V0.4), against the README and modules/talkinghead.mjs.
#
#   speakAudio(audio, [opt={}], [onsubtitles=null])
#   audio = {audio, words[], wtimes[], wdurations[], visemes[], vtimes[], vdurations[], anim}
#
# **All time values are MILLISECONDS.** Our `VisemeTimeline.as_message()` already emits ms
# (vtimes[0] == 100.0 for a 0.1 s prePhonemeLength), so no conversion at the WS boundary. This
# was the single most dangerous unknown in V0.4: seconds would have been a silent 1000x drift.
#
# `visemes[]` holds BARE Oculus ids, not the `viseme_`-prefixed morph target names.
TALKINGHEAD_VISEMES = ("aa", "E", "I", "O", "U", "PP", "SS", "TH", "DD", "FF",
                       "kk", "nn", "RR", "CH", "sil")
# Our mapper emits 14 of those 15 — everything except TH, which Japanese has no sound for.
#
# Moods are a CLOSED set, and "thinking"/"surprised"/"serious" are NOT in it. An unknown mood
# name is a silent no-op, so those three tags drive `neutral` plus explicit blendshape overrides
# (spec §8), never a mood string.
TALKINGHEAD_MOODS = ("neutral", "happy", "angry", "sad", "fear", "disgust", "love", "sleep")
# Eye contact is a pair of [0,1] options whose defaults are LOW — avatarIdleEyeContact 0.2 and
# avatarSpeakingEyeContact 0.5 — so an untouched avatar looks away most of the time. A tutor
# should hold the student's gaze; 0.9/0.9 does that while leaving enough drift to avoid a stare.
TALKINGHEAD_EYE_CONTACT_DEFAULTS = {"idle": 0.2, "speaking": 0.5}
TALKINGHEAD_GESTURES = ("handup", "index", "ok", "thumbup", "thumbdown", "side", "shrug")
# Blendshape override: head.setFixedValue("jawOpen", 1) and setFixedValue(name, null) to release;
# or an `anim` object {dt: [ms], vs: {shape: [values]}} inside speakAudio for audio-synced motion.
# Avatar: full-body GLB, Mixamo-compatible rig, ARKit (52 shapes) + Oculus visemes (15).
# Constructor: cameraView is one of "full" | "mid" | "upper" | "head"; lipsyncModules defaults to
# ["en","fi","lt"] and we pass [] because we always supply visemes ourselves.

# Verified live against VOICEVOX 0.25.2 /openapi.json on 2026-09-09.
# The engine loads a style's model on FIRST USE, so `GET /version` answering 200 means the
# engine is running, NOT that it can speak. Without an explicit warm-up the student's very
# first sentence pays that load as silence, which is indistinguishable from a hang.
#   POST /initialize_speaker     ?speaker=<style_id>&skip_reinit=<bool> -> 204 No Content
#   GET  /is_initialized_speaker ?speaker=<style_id>                    -> true|false
# Measured 2026-09-09 (style 53): forced reinit 534 ms; skip_reinit=true when already
# loaded 2 ms. `speaker` here is a STYLE id — the same id audio_query/synthesis take — so
# every distinct style in the emotion table needs its own call.
# Verified 2026-09-10, speaker 53: BOTH prePhonemeLength and postPhonemeLength are divided by
# speedScale. (post 0.0->0.5 adds 0.5013 s at speed 1.0, 0.2453 s at speed 2.0.) The engine also
# rounds each mora to sample boundaries, so a synthesised WAV runs ~10 ms longer than exact
# division predicts — the viseme timeline cannot match it to better than ~25 ms, and that residual
# is the engine's, not the mapper's. Do not "fix" visemes.py to chase it.
VOICEVOX_INIT_SPEAKER = "/initialize_speaker"
VOICEVOX_IS_INITIALIZED = "/is_initialized_speaker"
