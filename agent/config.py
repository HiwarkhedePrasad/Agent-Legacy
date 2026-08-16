import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _project_root() -> Path:
    """Where user-facing data lives (.env, memory/, workspace/, checkpoints).

    In source runs that's the repo root. In a packaged single-file binary,
    __file__ points inside PyInstaller's temp extraction dir (wiped on exit),
    so data is anchored next to the executable instead — override with
    AGENT_LEGACY_HOME. Distribution = agent-legacy.exe + .env sidecar.
    """
    if getattr(sys, "frozen", False):
        override = os.getenv("AGENT_LEGACY_HOME")
        if override:
            return Path(override).expanduser().resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _project_root()
load_dotenv(PROJECT_ROOT / ".env")


def _edit_root() -> Path:
    """Directory the agent is allowed to read/edit code in.

    Defaults to the sandboxed workspace/ folder. Set AGENT_LEGACY_EDIT_ROOT
    to point the filesystem tools (read_file / edit_file / write_file / grep /
    glob) at a real repository, so the agent can modify or add features in
    actual source files instead of only writing deliverables to workspace/.
    """
    override = os.getenv("AGENT_LEGACY_EDIT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return PROJECT_ROOT / "workspace"


class Settings:
    BASE_URL: str = os.getenv("BASE_URL", "https://api.openai.com/v1")
    API_KEY: str = os.getenv("API_KEY", "none")
    MODEL: str = os.getenv("MODEL", "gpt-4o-mini")
    TEMPERATURE: float = float(os.getenv("TEMPERATURE", "0.3"))

    SIMPLE_MODEL: str = os.getenv("SIMPLE_MODEL", "")
    SIMPLE_BASE_URL: str = os.getenv("SIMPLE_BASE_URL", "")
    SIMPLE_API_KEY: str = os.getenv("SIMPLE_API_KEY", "")

    MEDIUM_MODEL: str = os.getenv("MEDIUM_MODEL", "")
    MEDIUM_BASE_URL: str = os.getenv("MEDIUM_BASE_URL", "")
    MEDIUM_API_KEY: str = os.getenv("MEDIUM_API_KEY", "")

    COMPLEX_MODEL: str = os.getenv("COMPLEX_MODEL", "")
    COMPLEX_BASE_URL: str = os.getenv("COMPLEX_BASE_URL", "")
    COMPLEX_API_KEY: str = os.getenv("COMPLEX_API_KEY", "")

    MODEL_ROUTING: str = os.getenv("MODEL_ROUTING", "auto")  # auto|llm|heuristic

    # Mirror the model's chain-of-thought (reasoning_content) into the live log.
    # Off by default — reasoning tokens are verbose and can confuse the demo.
    SHOW_REASONING: bool = os.getenv("SHOW_REASONING", "0") in ("1", "true", "yes", "on")

    # Speech I/O via OpenRouter (TTS output + STT input).
    TTS_MODEL: str = os.getenv("TTS_MODEL", "fish-audio/s2.1-pro-free:free")
    STT_MODEL: str = os.getenv("STT_MODEL", "fish-audio/transcribe-1")

    TINYFISH_API_KEY: str = os.getenv("TINYFISH_API_KEY", "")
    TINYFISH_ENDPOINT: str = os.getenv("TINYFISH_ENDPOINT", "https://api.search.tinyfish.ai")

    MEMORY_DIR: Path = PROJECT_ROOT / "agent" / "data" / "memory"
    WORKSPACE_DIR: Path = PROJECT_ROOT / "workspace"

    # Root for filesystem tools (read_file/edit_file/write_file/...). See _edit_root.
    EDIT_ROOT: Path = _edit_root()

    # LangGraph checkpoint DB: persists run state (and therefore the ability to
    # RESUME after the step budget runs out) across restarts, on disk.
    CHECKPOINT_DB: Path = PROJECT_ROOT / "agent" / "data" / "checkpoints.sqlite"


settings = Settings()

_edit_root_env = os.getenv("AGENT_LEGACY_EDIT_ROOT")
if _edit_root_env and not settings.EDIT_ROOT.is_dir():
    print(
        f"[config] AGENT_LEGACY_EDIT_ROOT '{settings.EDIT_ROOT}' is not a "
        f"directory - falling back to {settings.WORKSPACE_DIR}",
        file=sys.stderr,
    )
    settings.EDIT_ROOT = settings.WORKSPACE_DIR
settings.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
settings.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
settings.CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
