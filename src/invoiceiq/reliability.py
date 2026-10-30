"""Reliability (spec §14): which model failures we fall back on, how long we wait, how many at once,
and how a failure is reported.

Everything here is a native Pydantic AI mechanism, configured deliberately:
- `FallbackModel(..., fallback_on=TRANSIENT)`: Haiku → Sonnet only for errors another model could
  survive. The default (`ModelAPIError`) would also fall back on a bad API key or an oversized
  prompt, where Sonnet fails exactly the same way, just more expensively.
- `ConcurrencyLimitedModel` with one shared `ConcurrencyLimiter`: at most N requests in flight to
  Anthropic across all agents, whether they run in a batch, the API or the CLI.
- Two timeouts: `ModelSettings['request_timeout']` bounds one request, including SDK retries;
  `run(timeout=...)` bounds a whole agent run (several requests and tools) and raises `RunTimedOut`.
- Provider rate limits (429) are first retried with backoff by the Anthropic SDK itself;
  FallbackModel only sees the error once those retries are exhausted.
"""

from __future__ import annotations

from functools import cache

from pydantic_ai import ConcurrencyLimiter
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelAPIError,
    ModelConnectionError,
    ModelContextWindowExceededError,
    ModelRateLimitError,
    ModelRequestRejectedError,
    ModelRequestTimeout,
    ModelServerError,
    ModelUnavailableError,
    RunTimedOut,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.models import Model, infer_model
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.models.fallback import FallbackModel

from invoiceiq.config import Settings
from invoiceiq.shared.cost import BudgetExceeded

# Errors a *different* model might not hit: worth falling back on.
TRANSIENT: tuple[type[Exception], ...] = (
    ModelRateLimitError,
    ModelUnavailableError,
    ModelServerError,
    ModelConnectionError,
    ModelRequestTimeout,
)


@cache
def shared_limiter(max_running: int) -> ConcurrencyLimiter:
    """One limiter per process (per size), shared by every agent's model."""
    return ConcurrencyLimiter(max_running=max_running, name="anthropic")


def build_model(settings: Settings, model: str, *, fallback: str | None = None) -> Model:
    """The production model for an agent: optional fallback, inside the shared concurrency limit."""
    inner = FallbackModel(model, fallback, fallback_on=TRANSIENT) if fallback else infer_model(model)
    return ConcurrencyLimitedModel(inner, limiter=shared_limiter(settings.model_concurrency))


def error_category(exc: BaseException) -> str:
    """A short, stable label for why a run failed, recorded on the invoice and shown to operators."""
    if isinstance(exc, FallbackExceptionGroup):
        # Every model in the fallback chain failed: report the first underlying cause.
        return error_category(exc.exceptions[0])
    categories: list[tuple[type[BaseException], str]] = [
        (BudgetExceeded, "budget_exceeded"),
        (UsageLimitExceeded, "usage_limit"),
        (RunTimedOut, "timeout"),
        (ModelRequestTimeout, "timeout"),
        (ModelRateLimitError, "rate_limited"),
        (ModelUnavailableError, "provider_unavailable"),
        (ModelServerError, "provider_error"),
        (ModelConnectionError, "connection"),
        (ModelContextWindowExceededError, "context_window"),
        (ModelRequestRejectedError, "request_rejected"),
        (ModelAPIError, "provider_error"),
        (UnexpectedModelBehavior, "invalid_output"),
    ]
    for exc_type, label in categories:
        if isinstance(exc, exc_type):
            return label
    return "internal_error"


def describe_failure(exc: BaseException) -> str:
    return f"{error_category(exc)}: {type(exc).__name__}: {exc}"
