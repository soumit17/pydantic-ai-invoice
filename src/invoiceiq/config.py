"""Application settings, loaded from the environment and `.env` (spec §19.1)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: SecretStr | None = None

    # V2 model strings must carry a provider prefix ('anthropic:...'); a bare name raises.
    model_dev: str = "anthropic:claude-haiku-5-5"
    model_final: str = "anthropic:claude-sonnet-5-5"

    spend_cap_usd: float = Field(
        default=6.0, validation_alias=AliasChoices("P1_SPEND_CAP_USD", "SPEND_CAP_USD")
    )
    # V2 raised Anthropic's default max_tokens to the model maximum, so every agent sets this (Rule 0.7).
    max_tokens_default: int = 2048

    company_gstin: str = "19ABCDE1234F1Z5"
    auto_approve_limit_inr: int = 50_000
    price_drift_pct: float = 5.0

    logfire_token: SecretStr | None = None

    data_dir: Path = PROJECT_ROOT / "data"
    runs_dir: Path = PROJECT_ROOT / "runs"

    @property
    def costs_path(self) -> Path:
        return self.runs_dir / "costs.jsonl"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # The Anthropic provider reads ANTHROPIC_API_KEY from the process environment, not from .env,
    # so surface the key there. An already-exported key (or the test dummy) wins.
    if settings.anthropic_api_key is not None:
        os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key.get_secret_value())
    return settings
