"""Application settings.

Every default is chosen so the business flow runs on a machine with no Docker, no MySQL and
no LLM key. Switching to a real database / HTTP backend / model is env-only: see
docs/EXTENSION_DB_DOCKER.md.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

# src/shopping_backend/core/config.py -> project root (package is installed editable, so this is stable)
PROJECT_ROOT = Path(__file__).resolve().parents[3]

AgentMode = Literal["rule_based", "llm"]
BackendDriver = Literal["inprocess", "http", "http_java"]
CheckpointBackend = Literal["sqlite", "postgres", "memory"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- environment ---
    APP_ENV: Literal["dev", "staging", "prod", "test"] = "dev"
    LOG_LEVEL: str = "INFO"
    LOG_DIR: Path = Path("./logs")
    ALLOWED_ORIGINS: list[str] = ["*"]

    # --- agent execution ---
    AGENT_MODE: AgentMode = "rule_based"
    AGENT_INTERNAL_TOKEN: str = "dev-token"
    AGENT_TOTAL_TIMEOUT_S: float = 30.0

    # --- persistence ---
    DATABASE_URL: str = "sqlite:///./data/business.db"
    DATA_DIR: Path = Path("./data")
    CHECKPOINT_BACKEND: CheckpointBackend = "sqlite"
    CHECKPOINT_DB_PATH: Path = Path("./data/checkpoints.db")
    POSTGRES_CHECKPOINT_URL: str = ""  # only for CHECKPOINT_BACKEND=postgres (uv sync --extra postgres)
    SEED_ON_STARTUP: bool = True

    # --- data source driver ---
    BACKEND_DRIVER: BackendDriver = "inprocess"
    BUSINESS_API_BASE_URL: str = "http://127.0.0.1:8001"
    BUSINESS_API_TOKEN: str = ""
    BUSINESS_API_TIMEOUT_S: float = 5.0
    BUSINESS_API_MAX_RETRY: int = 2

    # --- LLM ---
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = ""
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_FALLBACK_MODEL: str = ""
    LLM_TIMEOUT_S: float = 20.0
    MAX_LLM_CALL_RETRIES: int = 2

    # --- guardrails / business limits ---
    MAX_GOODS_LIMIT: int = 20
    MAX_FENCED_CHARS: int = 8000
    GROUNDING_PRICE_TOLERANCE_CENTS: int = 1
    PREFERENCE_TIER1_CAP: int = 8
    MAX_SEEN_GOODS_IDS: int = 50

    # --- ports ---
    AGENT_PORT: int = 8000
    BUSINESS_PORT: int = 8001

    # --- observability (LangSmith is pass-through env only, no SDK needed) ---
    LANGCHAIN_TRACING_V2: bool = False
    LANGSMITH_PROJECT: str = Field(default="shopping-backend")

    @property
    def is_dev(self) -> bool:
        return self.APP_ENV in ("dev", "test")

    @property
    def data_dir(self) -> Path:
        return self._abs(self.DATA_DIR)

    @property
    def log_dir(self) -> Path:
        return self._abs(self.LOG_DIR)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.OPENAI_API_KEY.strip())

    @property
    def effective_mode(self) -> AgentMode:
        """llm mode silently degrades to rule_based without a key: G7 must always hold."""
        return "llm" if (self.AGENT_MODE == "llm" and self.llm_enabled) else "rule_based"

    @property
    def business_auth_enabled(self) -> bool:
        return bool(self.BUSINESS_API_TOKEN.strip())

    @property
    def is_sqlite(self) -> bool:
        return make_url(self.DATABASE_URL).get_backend_name() == "sqlite"

    @property
    def resolved_database_url(self) -> str:
        """Rewrite relative sqlite paths against the project root so CWD never matters."""
        url = make_url(self.DATABASE_URL)
        if url.get_backend_name() != "sqlite" or not url.database:
            return self.DATABASE_URL
        if url.database == ":memory:":
            return self.DATABASE_URL
        path = Path(url.database)
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path.as_posix()}"

    @property
    def resolved_checkpoint_db_path(self) -> Path:
        path = self._abs(self.CHECKPOINT_DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _abs(path: Path) -> Path:
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    def safe_summary(self) -> dict[str, object]:
        """Startup log payload — never contains secrets."""
        return {
            "app_env": self.APP_ENV,
            "mode": self.effective_mode,
            "requested_mode": self.AGENT_MODE,
            "llm_enabled": self.llm_enabled,
            "backend_driver": self.BACKEND_DRIVER,
            "database": make_url(self.resolved_database_url).render_as_string(hide_password=True),
            "checkpointer": self.CHECKPOINT_BACKEND,
            "token_required": bool(self.AGENT_INTERNAL_TOKEN.strip()),
            "business_token_required": self.business_auth_enabled,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
