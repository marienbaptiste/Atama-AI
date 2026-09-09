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
# `--effort <low|medium|high|xhigh|max>` is the lever for spec §10's "extended thinking OFF":
# a default-effort haiku turn spent 602 characters thinking to produce 41 characters of speech.
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
# Pinned at V0.3 / V0.4 (M2/M3). Not yet verified — do not add guesses here.
