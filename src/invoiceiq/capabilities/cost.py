"""CostHooks (spec §8.2): the project-wide spend cap and cost ledger, as one `Hooks` capability.

`Hooks` (not a toolset or a wrapper model) because cost tracking is cross-cutting lifecycle
behaviour: it must see every run start and every model response of any agent it is attached to.
It is `Hooks[object]`: the deps type parameter is contravariant, so one instance fits every agent.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import Hooks
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition

from invoiceiq.config import Settings
from invoiceiq.shared.cost import check_budget, record_response

log = logging.getLogger("invoiceiq.cost")


def _agent_name(ctx: RunContext[Any]) -> str:
    return (ctx.agent.name if ctx.agent else None) or "agent"


def build_cost_hooks(settings: Settings) -> Hooks[object]:
    hooks: Hooks[object] = Hooks(id="cost", description="Project spend cap and per-request cost ledger")

    @hooks.on.before_run
    async def enforce_budget(ctx: RunContext[Any]) -> None:
        # Raising here aborts the run before any model request is made, so a capped project spends $0 more.
        check_budget(path=settings.costs_path, cap_usd=settings.spend_cap_usd)

    @hooks.on.after_model_request
    async def record_cost(
        ctx: RunContext[Any], *, request_context: ModelRequestContext, response: ModelResponse
    ) -> ModelResponse:
        # One ledger line per model response (so retries are counted), priced by genai-prices.
        cost = record_response(response, path=settings.costs_path, label=_agent_name(ctx))
        log.info(
            "%s request %d: in=%d out=%d cache_read=%d $%s",
            _agent_name(ctx),
            ctx.run_step,
            response.usage.input_tokens,
            response.usage.output_tokens,
            response.usage.cache_read_tokens,
            cost,
        )
        return response  # observe only: hand back the response unchanged

    @hooks.on.tool_execute
    async def time_tool(
        ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: Any, handler: Any
    ) -> Any:
        # A wrap hook: we run the tool ourselves via `handler`, so we can time it.
        start = time.perf_counter()
        try:
            return await handler(args)
        finally:
            log.info("tool %s took %.1f ms", call.tool_name, (time.perf_counter() - start) * 1000)

    @hooks.on.model_request_error
    async def log_error_category(
        ctx: RunContext[Any], *, request_context: ModelRequestContext, error: Exception
    ) -> ModelResponse:
        # The exception class *is* the category (ModelRateLimitError, ModelUnavailableError, ...; §14).
        log.warning("%s model request failed: %s: %s", _agent_name(ctx), type(error).__name__, error)
        raise error  # re-raise: recovery (fallback models, backoff) is M7's job

    return hooks
