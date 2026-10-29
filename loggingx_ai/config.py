"""Settings loaded from LOGGINGX_AI_* environment variables or a .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration; production requires an API key for callers."""

    model_config = SettingsConfigDict(
        env_prefix="LOGGINGX_AI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    # Shared secret loggingx-java sends in the X-API-Key header.
    api_key: SecretStr | None = None

    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    qwen_api_key: SecretStr | None = None
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"

    # Model of each agent as "provider:model"; an unconfigured provider falls back to the other one.
    log_analyst_model: str = "deepseek:deepseek-chat"
    code_analyst_model: str = "qwen:qwen3-coder-plus"
    root_cause_model: str = "deepseek:deepseek-chat"
    fixer_model: str = "qwen:qwen3-coder-plus"
    reviewer_model: str = "qwen:qwen-plus"

    temperature: float = Field(default=0.2, ge=0, le=2)
    request_timeout: float = Field(default=120, ge=5, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)
    # How often the reviewer may send a fix back to the fixer.
    max_review_rounds: int = Field(default=1, ge=0, le=3)

    # Folder holding loggingx-java's clones (store-<id>); requests that send no code are matched against it.
    repo_root: Path | None = None
    max_code_refs: int = Field(default=5, ge=1, le=20)
    code_context_lines: int = Field(default=30, ge=5, le=200)
    max_code_chars: int = Field(default=24_000, ge=1_000, le=200_000)

    @model_validator(mode="after")
    def validate_production(self) -> Settings:
        if self.environment == "production" and self.api_key is None:
            raise ValueError("LOGGINGX_AI_API_KEY must be configured in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
