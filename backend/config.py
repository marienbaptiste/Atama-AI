"""Configuration (spec §11, ADR-022).

Resolution order: built-in defaults -> settings.json -> environment variables.
One schema drives config, the settings page, and .env.example (a test asserts they match).
Secrets never leave the backend in full: use `hint()` for display.
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
    Setting("CLAUDE_MODEL", "sonnet", str, "model", "Model alias for the tutor subprocess"),
    Setting("CLAUDE_FALLBACK_MODEL", "haiku", str, "model", "Fallback when the model is overloaded / rate limited"),
    Setting("CLAUDE_TURN_TIMEOUT_S", 60, int, "model", "Per-turn timeout before SIGINT + apology"),
    Setting("CLAUDE_CWD", "", str, "advanced", "Dir the claude subprocess runs in. Empty = platform default OUTSIDE the repo (Claude Code walks up the tree for CLAUDE.md and keys project memory by it — verified 2026-09-09)"),
    # Network
    Setting("HOST", "127.0.0.1", str, "advanced", "Bind address. Loopback only (ADR-017)"),
    Setting("PORT", 8000, int, "advanced", "Orchestrator port"),
    Setting("VOICEVOX_URL", "http://127.0.0.1:50021", str, "advanced", "VOICEVOX engine (local Docker)"),
    # Voice
    Setting("VOICEVOX_SPEAKER", 1, int, "voice", "Base style id (real ids from GET /speakers)"),
    Setting("VOICEVOX_SPEED_SCALE", 0.9, float, "voice", "Default speech speed for learners"),
    Setting("VOICEVOX_INTONATION_SCALE", 1.0, float, "voice", "Default intonation"),
    Setting("EMOTION_HAPPY", "style=,speed=0.95,pitch=0.02,intonation=1.15", str, "voice", "Emotion -> voice params"),
    Setting("EMOTION_THINKING", "style=,speed=0.85,pitch=-0.01,intonation=0.90", str, "voice", "Emotion -> voice params"),
    Setting("EMOTION_SURPRISED", "style=,speed=1.00,pitch=0.04,intonation=1.30", str, "voice", "Emotion -> voice params"),
    Setting("EMOTION_SERIOUS", "style=,speed=0.85,pitch=-0.03,intonation=0.85", str, "voice", "Emotion -> voice params"),
    # Speech detection
    Setting("WHISPER_MODEL", "large-v3", str, "speech", "faster-whisper model (fallback: medium)"),
    Setting("WHISPER_COMPUTE_TYPE", "int8_float16", str, "speech", "CTranslate2 compute type"),
    Setting("VAD_SILENCE_MS", 600, int, "speech", "Silence that ends an utterance"),
    Setting("VAD_MIN_SPEECH_MS", 300, int, "speech", "Minimum speech before an utterance counts"),
    Setting("BARGEIN_THRESHOLD_FACTOR", 2.0, float, "speech", "VAD threshold multiplier while the avatar speaks"),
    Setting("BARGEIN_MIN_SPEECH_MS", 250, int, "speech", "Sustained speech required to barge in"),
    Setting("PLAYBACK_ONSET_IGNORE_MS", 150, int, "speech", "Ignore VAD at playback onset"),
    # Latency / VRAM guards
    Setting("FILLER_AFTER_MS", 1200, int, "advanced", "Play a filler if the first sentence has not closed"),
    Setting("LATENCY_WARN_S", 3.0, float, "advanced", "Warn when a turn exceeds this"),
    Setting("VRAM_WARN_GB", 10, int, "advanced", "Warn above this GPU memory use"),
    # Display
    Setting("SUBTITLES", "jp", str, "display", "jp | off"),
    Setting("STATUS_HEARTBEAT_S", 30, int, "display", "service_status heartbeat"),
    # Files
    Setting("SETTINGS_FILE", "settings.json", str, "advanced", "Config store (git-ignored, 0600)"),
    Setting("LOG_DIR", "logs", str, "advanced", "Session logs"),
    Setting("CACHE_DIR", ".cache", str, "advanced", "Generated/personal files"),
    Setting("SRS_CACHE_TTL_S", 3600, int, "advanced", "SRS disk cache TTL"),
    Setting("SRS_FETCH_BUDGET_S", 10, int, "advanced", "Session-start fetch budget"),
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


def load(settings_file: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> Config:
    if env is None:
        # Layer order: defaults < settings.json < .env file < real environment.
        env = {**read_dotenv(REPO_ROOT / ".env"), **os.environ}
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
