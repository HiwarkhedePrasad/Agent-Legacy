import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


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

    # Speech I/O via OpenRouter (TTS output + STT input).
    TTS_MODEL: str = os.getenv("TTS_MODEL", "fish-audio/s2.1-pro-free:free")
    STT_MODEL: str = os.getenv("STT_MODEL", "fish-audio/transcribe-1")

    TINYFISH_API_KEY: str = os.getenv("TINYFISH_API_KEY", "")
    TINYFISH_ENDPOINT: str = os.getenv("TINYFISH_ENDPOINT", "https://api.tinyfish.ai/v1/search")

    MEMORY_DIR: Path = PROJECT_ROOT / "agent" / "data" / "memory"
    WORKSPACE_DIR: Path = PROJECT_ROOT / "workspace"


settings = Settings()
settings.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
settings.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
