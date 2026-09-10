"""Configuration (spec §11, ADR-022).

Resolution order: built-in defaults -> settings.json -> .env file -> ATAMA_-prefixed env vars.
One schema drives config, the settings page, and .env.example (a test asserts they match).
Secrets never leave the backend in full: use `hint()` for display.

Why the prefix on the last layer: our keys are named after what they configure (CLAUDE_MODEL,
CLAUDE_EFFORT...), and Claude Code exports variables of its own with those exact names — running
the app from inside a Claude Code session picked up CLAUDE_EFFORT=high from the parent shell
(found 2026-09-09). The `.env` file is ours and keeps bare names; the process environment, which
we do not own, is read only under `ATAMA_`.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Setting:
    key: str            # env-style name, e.g. WANIKANI_TOKEN; settings.json uses key.lower()
    default: Any
    type: type
    group: str
    description: str
    secret: bool = False


# fmt: off
SCHEMA: tuple[Setting, ...] = (
    # Account & tokens
    Setting("WANIKANI_TOKEN", "", str, "account", "WaniKani personal access token (create with NO write permissions)", secret=True),
    Setting("BUNPRO_API_TOKEN", "", str, "account", "Bunpro -> Settings -> API -> Account API Token", secret=True),
    Setting("CLAUDE_CODE_OAUTH_TOKEN", "", str, "account", "Optional; from `claude setup-token`. Empty = interactive login", secret=True),
    # Model
    Setting("BRAIN_PROVIDER", "claude-cli", str, "model", "Which brain implementation to use (ADR-027). Implemented: claude-cli. An OpenAI headless provider is planned (ROADMAP subsystem 20)"),
    Setting("CLAUDE_MODEL", "sonnet", str, "model", "Model tier for the tutor: sonnet | opus | haiku, each resolved to the NEWEST model your account can use (backend/data/model_tiers.txt, checked once a week). A full model id pins that exact model"),
    Setting("CLAUDE_EFFORT", "medium", str, "model", "CLI effort level (low|medium|high|xhigh|max). Measured 2026-09-09, median first-token BETWEEN turns / opening turn: low 2.75s/8.6s, medium 2.14s/5.8s, high 3.75s/19.2s. Medium is the floor, not a compromise: it is the fastest of the three on BOTH numbers, so dropping to low would buy nothing. Sonnet cannot turn thinking off at any level (docs, 2026-09-10); the Claude stage budget is 3.60s since ADR-033 (spec S10)"),
    Setting("CLAUDE_FALLBACK_MODEL", "haiku", str, "model", "Fallback when the model is overloaded / rate limited"),
    Setting("CLAUDE_REPLACE_SYSTEM_PROMPT", True, bool, "model", "true: --system-prompt (Sensei only). false: --append-system-prompt, which leaves Claude Code's coding-agent prompt in front and breaks the persona"),
    Setting("CLAUDE_TURN_TIMEOUT_S", 60, int, "model", "Per-turn timeout before SIGINT + apology"),
    Setting("CLAUDE_CWD", "", str, "advanced", "Dir the claude subprocess runs in. Empty = platform default OUTSIDE the repo (Claude Code walks up the tree for CLAUDE.md and keys project memory by it — verified 2026-09-09)"),
    # Network
    Setting("HOST", "127.0.0.1", str, "advanced", "Bind address. Loopback only (ADR-017)"),
    Setting("PORT", 8000, int, "advanced", "Orchestrator port"),
    Setting("VOICEVOX_URL", "http://127.0.0.1:50021", str, "advanced", "VOICEVOX engine (local Docker)"),
    Setting("SEARXNG_URL", "http://127.0.0.1:8888", str, "advanced", "Self-hosted SearxNG for the tutor's search tool (ADR-028). News also draws on the headline feeds in backend/data/news_feeds.txt"),
    # Voice
    Setting("VOICEVOX_SPEAKER", -1, int, "voice", "Base VOICEVOX style id. -1 = take it from the persona, which is what you usually want. 53 = 麒ヶ島宗麟 (たなか), 67 = 栗田まろん (はやし), 29 = No.7 (みなみ), 14 = 冥鳴ひまり (ゆい)"),
    Setting("VOICEVOX_SPEED_SCALE", 0.9, float, "voice", "Default speech speed for learners"),
    Setting("VOICEVOX_INTONATION_SCALE", 1.0, float, "voice", "Default intonation. Above 1 is livelier, below 1 flatter"),
    Setting("VOICEVOX_PITCH_SCALE", 0.0, float, "voice", "Baseline pitch shift for every emotion. Negative lowers the register: -0.06 to -0.12 makes a well-trained female voice read as male without losing its quality"),
    Setting("VOICEVOX_PRE_PHONEME", 0.0, float, "voice", "Lead-in silence per sentence (s). VOICEVOX defaults to 0.1, but we synthesise sentence by sentence, so it lands BETWEEN sentences as dead air"),
    Setting("VOICEVOX_POST_PHONEME", 0.08, float, "voice", "Trailing silence per sentence (s). Enough to breathe, not enough to sound chopped"),
    Setting("VOICEVOX_PAUSE_SCALE", 1.0, float, "voice", "Multiplies the pauses at 、 and 。 Higher is more measured"),
    Setting("EMOTION_HAPPY", "", str, "voice", "Override for this emotion, e.g. style=31,speed=1.05,pitch=0.02,intonation=1.15. Empty = the built-in table in backend/emotions.py, which picks styles by name"),
    Setting("EMOTION_THINKING", "", str, "voice", "Override for this emotion, e.g. style=31,speed=1.05,pitch=0.02,intonation=1.15. Empty = the built-in table in backend/emotions.py, which picks styles by name"),
    Setting("EMOTION_SURPRISED", "", str, "voice", "Override for this emotion, e.g. style=31,speed=1.05,pitch=0.02,intonation=1.15. Empty = the built-in table in backend/emotions.py, which picks styles by name"),
    Setting("EMOTION_SERIOUS", "", str, "voice", "Override for this emotion, e.g. style=31,speed=1.05,pitch=0.02,intonation=1.15. Empty = the built-in table in backend/emotions.py, which picks styles by name"),
    Setting("EMOTION_ENCOURAGING", "", str, "voice", "Override for this emotion, e.g. style=31,speed=1.05,pitch=0.02,intonation=1.15. Empty = the built-in table in backend/emotions.py, which picks styles by name"),
    Setting("EMOTION_PROUD", "", str, "voice", "Override for this emotion, e.g. style=31,speed=1.05,pitch=0.02,intonation=1.15. Empty = the built-in table in backend/emotions.py, which picks styles by name"),
    Setting("EMOTION_CONFUSED", "", str, "voice", "Override for this emotion, e.g. style=31,speed=1.05,pitch=0.02,intonation=1.15. Empty = the built-in table in backend/emotions.py, which picks styles by name"),
    # Speech detection
    Setting("WHISPER_MODEL", "large-v3", str, "speech", "faster-whisper model (fallback: medium)"),
    Setting("WHISPER_COMPUTE_TYPE", "float16", str, "speech", "CTranslate2 compute type. float16 is the default because int8 buys nothing on a GPU that does fp16 natively: measured 2026-09-09, same median latency (287 vs 290 ms) and better transcripts, for 1.7 GB of headroom we were not spending. int8_float16 is the fallback if VRAM ever gets tight"),
    Setting("TURN_MODE", "ptt", str, "speech", "How a turn ends: ptt (you press a key - reliable, and the tutor's own voice can never end your turn) or vad (silence ends it - hands-free, but see VAD_SILENCE_MS)"),
    Setting("VAD_SILENCE_MS", 900, int, "speech", "Silence that ends an utterance. Raised 600 -> 900 on 2026-09-10: 600 ms cut the student off mid-thought. It is spent DIRECTLY from the 5.0 s voice->voice budget (spec S10 allots 0.50 s to this stage), so raising it buys patience with latency"),
    Setting("VAD_MIN_SPEECH_MS", 300, int, "speech", "Minimum speech before an utterance counts"),
    Setting("BARGEIN_THRESHOLD_FACTOR", 2.0, float, "speech", "VAD threshold multiplier while the avatar speaks"),
    Setting("BARGEIN_MIN_SPEECH_MS", 250, int, "speech", "Sustained speech required to barge in"),
    Setting("PLAYBACK_ONSET_IGNORE_MS", 150, int, "speech", "Ignore VAD at playback onset"),
    # Latency / VRAM guards
    Setting("FILLER_AFTER_MS", 1200, int, "advanced", "Play a filler if the first sentence has not closed"),
    Setting("LATENCY_WARN_S", 5.0, float, "advanced", "Warn when a turn exceeds this. 5.0 since 2026-09-10 (ADR-033): the gate is voice->voice p90 <= 5.0 s"),
    Setting("VRAM_WARN_GB", 10, int, "advanced", "Warn above this GPU memory use"),
    Setting("CONTEXT_ROTATE_AT", 0.0, float, "advanced", "Start a fresh session (with the lesson so far) when her context passes this fraction of the model's window, before the provider compacts on its own - which is seconds of silence mid-lesson (ADR-032). 0 = never. The window is 200k tokens (verified); where the provider compacts is not measured yet (ROADMAP V0.12), so keep this well below it, e.g. 0.7"),
    # Display
    Setting("TUTOR_PERSONA", "minami", str, "voice", "Which tutor: a name in prompts/ (tanaka, hayashi, minami, mori) or a path. The persona declares its own voice AND its avatar, so this one setting switches character, voice and face together (ADR-026/030). Default is minami because she is the one with a shipped avatar"),
    Setting("SUBTITLES", "jp", str, "display", "jp | off"),
    Setting("AUDIO_INPUT_DEVICE", "", str, "audio", "Microphone, by name. Empty = system default. If it is unplugged, or not there at launch, the app uses the system default and switches back when it returns (spec §9)"),
    Setting("AUDIO_OUTPUT_DEVICE", "", str, "audio", "Speakers/headphones for the terminal voice (--speak), by name. Empty = system default. If they are unplugged, or not there at launch, the app uses the system default and switches back when they return (spec §9). On the avatar page her voice plays through the browser, which follows the OS default output"),
    Setting("STATUS_HEARTBEAT_S", 30, int, "display", "service_status heartbeat"),
    # Files
    Setting("SETTINGS_FILE", "settings.json", str, "advanced", "Config store (git-ignored, 0600)"),
    Setting("LOG_DIR", "logs", str, "advanced", "Session logs"),
    Setting("MEMORY_ENABLED", True, bool, "model", "Cross-session memory (spec §6b): the tutor remembers last session, avoids recently discussed topics, and keeps notes about you in an editable file outside the repo"),
    Setting("MEMORY_SUMMARY_MODEL", "haiku", str, "model", "Model that summarises each past session at the next launch. Cheap on purpose: it runs once per session over a short excerpt, never on the conversation path"),
    Setting("CACHE_DIR", ".cache", str, "advanced", "Generated/personal files"),
    Setting("SRS_CACHE_TTL_S", 3600, int, "advanced", "SRS disk cache TTL"),
    Setting("SRS_FETCH_BUDGET_S", 10, int, "advanced", "Session-start fetch budget, PER SERVICE (they fetch in parallel). Measured 2026-09-09: bunpro 5.1s, wanikani 4.3s, so 10s is roughly 2x headroom. On timeout the last snapshot is served as stale, never dropped"),
)
# fmt: on

KEYS: tuple[str, ...] = tuple(s.key for s in SCHEMA)
_BY_KEY = {s.key: s for s in SCHEMA}
SECRET_KEYS: frozenset[str] = frozenset(s.key for s in SCHEMA if s.secret)


def _coerce(setting: Setting, raw: Any) -> Any:
    if raw is None:
        return setting.default
    if setting.type is bool:
        return str(raw).lower() in ("1", "true", "yes", "on")
    try:
        return setting.type(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{setting.key}: expected {setting.type.__name__}, got {raw!r}") from e


class Config:
    """Resolved configuration. Attribute access by env-style key: cfg.WANIKANI_TOKEN."""

    def __init__(self, values: dict[str, Any], settings_path: Path, first_run: bool):
        self._values = values
        self.settings_path = settings_path
        self.first_run = first_run

    def __getattr__(self, key: str) -> Any:
        try:
            return self._values[key]
        except KeyError:
            raise AttributeError(key) from None

    def get(self, key: str) -> Any:
        return self._values[key]

    def secrets(self) -> dict[str, str]:
        """Secret values that are set. For handing to subprocess env blocks / redaction."""
        return {k: v for k, v in self._values.items() if k in SECRET_KEYS and v}

    def public_view(self) -> dict[str, Any]:
        """What the settings page / logs may see: secrets replaced by {set, hint}."""
        out: dict[str, Any] = {}
        for k, v in self._values.items():
            out[k] = {"set": bool(v), "hint": hint(v)} if k in SECRET_KEYS else v
        return out

    def path(self, key: str) -> Path:
        """Resolve a path-valued setting relative to the repo root."""
        return (REPO_ROOT / str(self._values[key])).resolve()


def claude_cwd(cfg: "Config") -> Path:
    """Working directory for the claude subprocess (spec §4). MUST be outside the repository:
    Claude Code discovers CLAUDE.md in ancestor directories and keys its project memory by the
    detected project root, so `.cache/claude-cwd` inside the repo was NOT isolated (init.memory_paths
    pointed at the Atama-AI project, 2026-09-09). Default: a per-user state dir; stable across
    restarts (session persistence is keyed by cwd, which --resume needs)."""
    configured = str(cfg.get("CLAUDE_CWD") or "").strip()
    if configured:
        p = Path(configured).expanduser()
        p = p if p.is_absolute() else (REPO_ROOT / p).resolve()
        if p == REPO_ROOT or REPO_ROOT in p.parents:
            import warnings
            warnings.warn(f"CLAUDE_CWD={configured!r} is inside the repository and would expose CLAUDE.md to the tutor; "
                          "using the platform default instead", RuntimeWarning, stacklevel=2)
        else:
            return p
    if os.name == "nt":
        # NOT %LOCALAPPDATA%: the Microsoft Store Python build virtualises writes under AppData
        # (MSIX), so a directory it creates there is invisible to cmd.exe/claude ("The current
        # directory is invalid.", 2026-09-09). The profile root is not virtualised.
        base = Path.home() / ".atama-ai"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "atama-ai"
    return base / "claude-cwd"


def hint(value: str) -> str:
    return ("…" + value[-4:]) if value and len(value) >= 8 else ("set" if value else "")


def read_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=value lines, `#` comments, optional quotes. No expansion."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            out[key] = value
    return out


ENV_PREFIX = "ATAMA_"


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Process-environment overrides, read ONLY under `ATAMA_` (see module docstring)."""
    environ = os.environ if environ is None else environ
    return {k[len(ENV_PREFIX):]: v for k, v in environ.items()
            if k.startswith(ENV_PREFIX) and k[len(ENV_PREFIX):] in _BY_KEY}


def load(settings_file: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> Config:
    if env is None:
        # defaults < settings.json < .env (bare names, our file) < ATAMA_* (not our namespace)
        env = {**read_dotenv(REPO_ROOT / ".env"), **env_overrides()}
    settings_path = Path(settings_file or env.get("SETTINGS_FILE") or _BY_KEY["SETTINGS_FILE"].default)
    if not settings_path.is_absolute():
        settings_path = REPO_ROOT / settings_path
    stored: dict[str, Any] = {}
    first_run = not settings_path.exists()
    if not first_run:
        stored = json.loads(settings_path.read_text(encoding="utf-8") or "{}")
    values: dict[str, Any] = {}
    for s in SCHEMA:
        raw: Any = s.default
        if s.key.lower() in stored:
            raw = stored[s.key.lower()]
        if s.key in env and env[s.key] != "":
            raw = env[s.key]
        values[s.key] = _coerce(s, raw)
    return Config(values, settings_path, first_run)


def save(updates: dict[str, Any], settings_file: str | os.PathLike | None = None) -> Path:
    """Merge `updates` (env-style keys) into settings.json atomically, mode 0600."""
    cfg = load(settings_file)
    path = cfg.settings_path
    current: dict[str, Any] = {}
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8") or "{}")
    for key, value in updates.items():
        if key not in _BY_KEY:
            raise KeyError(f"unknown setting {key}")
        current[key.lower()] = _coerce(_BY_KEY[key], value)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass  # Windows: ACLs, not modes
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path
