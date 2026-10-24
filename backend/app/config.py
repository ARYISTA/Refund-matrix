"""Application settings, read from environment variables (and the repo-root .env file).

Secrets live in env only. `database_url` is a SecretStr because it contains the password.
Settings ignore unknown variables so .env can already hold values for later phases.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.contracts.enums import ProviderName
from app.policy.config import DEFAULT_POLICY_PATH, PolicyConfig, load_policy

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # Relative paths are resolved against backend/.
    policy_path: Path = DEFAULT_POLICY_PATH
    # No default on purpose: the app must not start against an unintended database.
    database_url: SecretStr

    # LLM (Phase 3). Model names are config, not code; they are checked when the provider is
    # built, so settings for other phases do not need them.
    llm_provider: ProviderName = ProviderName.GROQ
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    llm_max_retries: int = Field(default=3, ge=0, le=10)
    # A Retry-After longer than this is not waited out; the caller falls back instead.
    llm_max_retry_wait_seconds: float = Field(default=20.0, ge=0, le=300)
    groq_api_key: SecretStr | None = None
    groq_model_triage: str | None = None
    groq_model_agent: str | None = None
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = Field(default=384, gt=0, le=4096)
    rag_min_similarity: float = Field(default=0.35, ge=0, le=1)
    rag_top_k: int = Field(default=5, ge=1, le=50)
    tracing_enabled: bool = False
    tracing_sample_rate: float = Field(default=1.0, ge=0, le=1)
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    payout_gateway: Literal["mock"] = "mock"
    # Development and unit-test instances may use the deterministic fallback; production
    # still requires an explicitly configured secret.
    jwt_secret: SecretStr = Field(
        default=SecretStr("development-only-jwt-secret-change-me-32"),
        min_length=32,
    )
    jwt_expire_minutes: int = Field(default=60, gt=0, le=1440)
    cookie_secure: bool = False
    cors_allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    rate_limit_per_minute: int = Field(default=30, gt=0, le=1000)
    max_threshold_paise: int = Field(default=10_000_000, ge=100, le=2_147_483_647)

    @field_validator("database_url")
    @classmethod
    def _require_psycopg3_url(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must start with 'postgresql+psycopg://' (psycopg 3 driver)")
        return v

    @field_validator("policy_path")
    @classmethod
    def _resolve_policy_path(cls, v: Path) -> Path:
        return v if v.is_absolute() else (BACKEND_DIR / v).resolve()

    @field_validator("jwt_secret")
    @classmethod
    def _require_production_jwt_secret(cls, v: SecretStr, info: object) -> SecretStr:
        values = getattr(info, "data", {})
        if values.get("app_env") == "production" and v.get_secret_value().startswith("development-only-"):
            raise ValueError("JWT_SECRET must be explicitly configured in production")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_policy() -> PolicyConfig:
    """Load and validate policy.yaml. Call once at startup so a bad policy stops the app."""
    return load_policy(get_settings().policy_path)


def runtime_database_url(settings: Settings | None = None) -> str:
    """Prefer IPv4 for the local Docker binding on Windows hosts."""
    value = (settings or get_settings()).database_url.get_secret_value()
    parsed = make_url(value)
    if parsed.host == "localhost":
        parsed = parsed.set(host="127.0.0.1")
    return parsed.render_as_string(hide_password=False)
