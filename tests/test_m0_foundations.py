from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from invoiceiq.agents.hello import build_hello_agent
from invoiceiq.config import Settings
from invoiceiq.shared.cost import BudgetExceeded, check_budget, record_response, total_spend


def test_hello_agent_with_test_model(settings: Settings) -> None:
    agent = build_hello_agent(settings)
    # override() swaps the model for this block only; TestModel answers locally for $0.
    with agent.override(model=TestModel(custom_output_text="Hello from TestModel")):
        result = agent.run_sync("hi")
    assert result.output == "Hello from TestModel"
    assert result.usage.requests == 1


def test_real_model_requests_are_blocked(settings: Settings) -> None:
    agent = build_hello_agent(settings)
    with pytest.raises(RuntimeError, match="ALLOW_MODEL_REQUESTS"):
        agent.run_sync("this must never reach Anthropic")


def test_model_string_has_provider_prefix(settings: Settings) -> None:
    assert settings.model_dev.startswith("anthropic:")
    assert settings.max_tokens_default > 0


def _response(model: str, inp: int, out: int) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart("x")],
        usage=RequestUsage(input_tokens=inp, output_tokens=out),
        model_name=model,
        provider_name="anthropic",
    )


def test_cost_ledger_records_and_sums(settings: Settings) -> None:
    path = settings.costs_path
    c1 = record_response(_response("claude-haiku-5-5", 10_000, 0), path=path, label="t")
    c2 = record_response(_response("claude-haiku-5-5", 0, 10_000), path=path, label="t")
    assert c1 > 0 and c2 > c1  # output tokens cost more than input
    assert total_spend(path=path) == c1 + c2
    assert total_spend(path=path, project="other") == 0


def test_long_prompts_are_billed_at_the_higher_tier(settings: Settings) -> None:
    # Prompts over 100k tokens bill *every* token at 5x (genai-prices tiered pricing). Keep prompts short.
    short = record_response(_response("claude-haiku-5-5", 100_000, 0), path=settings.costs_path)
    long = record_response(_response("claude-haiku-5-5", 100_001, 0), path=settings.costs_path)
    assert long > short * 4


def test_unknown_model_is_logged_at_zero_cost(settings: Settings) -> None:
    cost = record_response(_response("not-a-real-model", 10, 10), path=settings.costs_path)
    assert cost == Decimal(0)
    assert settings.costs_path.read_text().count("\n") == 1


def test_budget_cap_raises(settings: Settings) -> None:
    path = settings.costs_path
    assert check_budget(path=path, cap_usd=0.01) == 0
    record_response(_response("claude-sonnet-5-5", 1_000_000, 0), path=path)
    with pytest.raises(BudgetExceeded):
        check_budget(path=path, cap_usd=0.01)
