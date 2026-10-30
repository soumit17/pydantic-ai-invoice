"""Fault injection (spec §14): FunctionModels that fail on purpose, and what the system does about it."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelAuthenticationError,
    ModelRateLimitError,
    ModelServerError,
    RunTimedOut,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from invoiceiq.agents.extractor import INVOICE_TOOL, ExtractDeps, build_extractor
from invoiceiq.config import Settings
from invoiceiq.pipeline.graph import InvoiceSource, pipeline_deps, process_invoice
from invoiceiq.reliability import TRANSIENT, error_category, shared_limiter
from tests.fakes import fake_auditor, fake_poster, runtime_settings, truth
from tests.test_m1_domain import US, WB_VENDOR, invoice

DEPS = ExtractDeps(company_gstin=US, today=date(2026, 10, 29))
GOOD = json.loads(invoice().model_dump_json(exclude={"kind"}))


def answer(args: dict[str, Any] | None = None) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(INVOICE_TOOL, args or GOOD)],
        usage=RequestUsage(input_tokens=10, output_tokens=10),
    )


def failing(exc: Exception, calls: list[str], name: str) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(name)
        raise exc

    return FunctionModel(fn, model_name=name)


def succeeding(calls: list[str], name: str) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(name)
        return answer()

    return FunctionModel(fn, model_name=name)


# --- invalid output, then valid: output retries ----------------------------------------------------


def test_invalid_output_twice_then_valid_succeeds(settings: Settings) -> None:
    bad = {**GOOD, "buyer": {**GOOD["buyer"], "gstin": WB_VENDOR}}
    responses = iter(
        [answer(bad), answer({**GOOD, "supplier": {"name": "x", "gstin": "NOT-A-GSTIN"}}), answer()]
    )
    model = FunctionModel(lambda messages, info: next(responses))
    result = build_extractor(settings, model=model).run_sync("doc", deps=DEPS)
    assert result.usage.requests == 3 and result.output.invoice_no == "INV/26/042"  # 1 try + 2 retries


def test_a_third_invalid_output_gives_up_cleanly(settings: Settings) -> None:
    bad = {**GOOD, "buyer": {**GOOD["buyer"], "gstin": WB_VENDOR}}
    model = FunctionModel(lambda messages, info: answer(bad))
    with pytest.raises(UnexpectedModelBehavior) as caught:
        build_extractor(settings, model=model).run_sync("doc", deps=DEPS)
    assert error_category(caught.value) == "invalid_output"


# --- provider errors: what falls back and what doesn't --------------------------------------------


def test_rate_limit_falls_back_to_the_second_model(settings: Settings) -> None:
    calls: list[str] = []
    model = FallbackModel(
        failing(ModelRateLimitError("haiku", "429 slow down"), calls, "haiku"),
        succeeding(calls, "sonnet"),
        fallback_on=TRANSIENT,
    )
    result = build_extractor(settings, model=model).run_sync("doc", deps=DEPS)
    assert calls == ["haiku", "sonnet"] and result.output.invoice_no == "INV/26/042"
    assert result.response.model_name == "sonnet"  # the trace and cost ledger show who answered


def test_auth_errors_do_not_fall_back(settings: Settings) -> None:
    # A bad API key fails on every model; falling back would only spend more.
    calls: list[str] = []
    model = FallbackModel(
        failing(ModelAuthenticationError("haiku", "401 invalid x-api-key"), calls, "haiku"),
        succeeding(calls, "sonnet"),
        fallback_on=TRANSIENT,
    )
    with pytest.raises(ModelAuthenticationError) as caught:
        build_extractor(settings, model=model).run_sync("doc", deps=DEPS)
    assert calls == ["haiku"] and error_category(caught.value) == "request_rejected"


def test_when_every_model_fails_the_first_cause_is_reported(settings: Settings) -> None:
    calls: list[str] = []
    model = FallbackModel(
        failing(ModelServerError("haiku", "500"), calls, "haiku"),
        failing(ModelRateLimitError("sonnet", "429"), calls, "sonnet"),
        fallback_on=TRANSIENT,
    )
    with pytest.raises(FallbackExceptionGroup) as caught:
        build_extractor(settings, model=model).run_sync("doc", deps=DEPS)
    assert calls == ["haiku", "sonnet"] and error_category(caught.value) == "provider_error"


# --- limits and timeouts --------------------------------------------------------------------------


def test_request_limit_stops_a_runaway_run(settings: Settings) -> None:
    from pydantic_ai.usage import UsageLimits

    bad = {**GOOD, "buyer": {**GOOD["buyer"], "gstin": WB_VENDOR}}  # never accepted
    model = FunctionModel(lambda messages, info: answer(bad))
    with pytest.raises(UsageLimitExceeded) as caught:
        build_extractor(settings, model=model).run_sync(
            "doc", deps=DEPS, usage_limits=UsageLimits(request_limit=2)
        )
    assert error_category(caught.value) == "usage_limit"


async def test_run_timeout(settings: Settings) -> None:
    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(5)
        return answer()

    with pytest.raises(RunTimedOut) as caught:
        await build_extractor(settings, model=FunctionModel(slow)).run("doc", deps=DEPS, timeout=0.2)
    assert error_category(caught.value) == "timeout"


async def test_concurrency_limit_caps_requests_in_flight() -> None:
    in_flight, peak = 0, 0

    async def tracked(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return ModelResponse(parts=[TextPart("ok")])

    from pydantic_ai import Agent

    limited = ConcurrencyLimitedModel(FunctionModel(tracked), limiter=shared_limiter(2))
    agent = Agent(limited)
    await asyncio.gather(*(agent.run(f"q{i}") for i in range(6)))
    assert peak == 2


# --- a failure inside the pipeline becomes a clean record -----------------------------------------


async def test_pipeline_failure_is_recorded_with_its_category(dataset: Path, tmp_path: Path) -> None:
    settings = runtime_settings(dataset, tmp_path)
    gt = truth(dataset, "inv-001")
    calls: list[str] = []
    broken = build_extractor(
        settings, model=failing(ModelAuthenticationError("haiku", "401 invalid x-api-key"), calls, "haiku")
    )
    deps = await pipeline_deps(
        settings, extractor=broken, auditor=fake_auditor(settings), poster=fake_poster(settings)
    )
    record = await process_invoice(InvoiceSource("inv-001", dataset / "invoices" / gt.file_name), deps)
    assert record.status == "failed" and record.error is not None
    assert record.error.startswith("request_rejected: ModelAuthenticationError")
    assert await deps.store.get_invoice("inv-001") == record  # stored, so the API and CLI can show it


def test_unknown_errors_are_internal() -> None:
    assert error_category(ValueError("bug")) == "internal_error"


# --- durable replays don't count twice in the cost ledger -----------------------------------------


def test_a_replayed_response_is_not_recorded_twice(tmp_path: Path) -> None:
    # After a crash, DBOS replays checkpointed responses through the agent, and so through the
    # after_model_request hook. Same provider_response_id: one ledger line, counted once (D22).
    from decimal import Decimal

    from invoiceiq.shared.cost import record_response, total_spend

    path = tmp_path / "costs.jsonl"
    response = ModelResponse(
        parts=[TextPart("ok")],
        usage=RequestUsage(input_tokens=1000, output_tokens=100),
        model_name="claude-haiku-5-5",
        provider_response_id="msg_replayed",
    )
    first = record_response(response, path=path, invoice="inv-016")
    assert first > 0 and record_response(response, path=path, invoice="inv-016") == Decimal(0)
    assert len(path.read_text().splitlines()) == 1 and total_spend(path=path) == first
    other = ModelResponse(parts=[TextPart("ok")], usage=response.usage, model_name="claude-haiku-5-5")
    record_response(other, path=path)  # no id (e.g. FunctionModel): always recorded
    record_response(other, path=path)
    assert len(path.read_text().splitlines()) == 3
