"""Application settings, loaded from the environment and `.env` (spec §19.1)."""

from __future__ import annotations

import os
from datetime import date
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

    # Mock data is dated relative to 2026-10-29. Pinning "today" keeps date checks and evals
    # reproducible; leave it unset to use the real date.
    as_of_date: date | None = None

    logfire_token: SecretStr | None = None

    # Ask-AP reaches the ledger over MCP: this URL (streamable HTTP), or in-process when unset.
    mcp_url: str | None = None
    # Re-run the auditor once with Thinking() when it disagrees with the rules (M6, D19).
    audit_escalation: bool = True
    # Share of auditor runs evaluated online (M8). 0 turns it off; tests use 1.0 or 0.
    online_eval_rate: float = 0.2
    # At most this many model requests in flight at once, across all agents (M7).
    model_concurrency: int = 4
    # Seconds: one model request (all SDK retries included), and one whole agent run (M7).
    request_timeout: float = 60.0
    run_timeout: float = 180.0
    # Durable batches (M7): DBOS keeps its workflow state here, inside data/runtime/.
    application_version: str = "invoiceiq-p1"

    @property
    def dbos_url(self) -> str:
        return f"sqlite:///{self.runtime_dir / 'dbos.sqlite'}"

    data_dir: Path = PROJECT_ROOT / "data"
    runs_dir: Path = PROJECT_ROOT / "runs"

    @property
    def costs_path(self) -> Path:
        return self.runs_dir / "costs.jsonl"

    @property
    def ledger_seed_path(self) -> Path:
        """The committed, read-only ledger written by the mock-data generator."""
        return self.data_dir / "ledger.sqlite"

    @property
    def runtime_dir(self) -> Path:
        """Mutable state (git-ignored): the working ledger, app.sqlite, later dbos.sqlite."""
        return self.data_dir / "runtime"

    @property
    def ledger_path(self) -> Path:
        """The working ledger that postings write to, copied from the seed on first use."""
        return self.runtime_dir / "ledger.sqlite"

    @property
    def app_db_path(self) -> Path:
        return self.runtime_dir / "app.sqlite"

    def today(self) -> date:
        return self.as_of_date or date.today()


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # The Anthropic provider reads ANTHROPIC_API_KEY from the process environment, not from .env,
    # so surface the key there. An already-exported key (or the test dummy) wins.
    if settings.anthropic_api_key is not None:
        os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key.get_secret_value())
    return settings
