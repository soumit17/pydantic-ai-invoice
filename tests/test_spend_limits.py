"""Harness SpendLimits vs our CostHooks (M8): what each one counts, and when each one stops a run.

CostHooks (M2) is the project's ledger and cap: a file every process shares, checked before each run.
SpendLimits is a brake: counted per response, checked before each *request*, with windows and
scopes, but its counters live in a store (in-memory here; Redis/Postgres to share them). D23.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness import SpendLimits, ToolCallJudge
from pydantic_ai_harness.spend import Budget, SpendLimitExceeded

from invoiceiq.capabilities.cost import build_cost_hooks
from invoiceiq.config import Settings
from invoiceiq.shared.cost import total_spend

USAGE = RequestUsage(input_tokens=1000, output_tokens=100)  # $0.00015 at Haiku 5.5 prices


def haiku_like(calls_before_answer: int) -> FunctionModel:
    """Makes `calls_before_answer` tool calls, then answers. Priced as Haiku by genai-prices."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        done = sum(isinstance(m, ModelResponse) for m in messages)
        part = ToolCallPart("lookup", {"q": "x"}) if done < calls_before_answer else TextPart("done")
        return ModelResponse(
            parts=[part], usage=USAGE, model_name="claude-haiku-5-5", provider_name="anthropic"
        )

    # model_name on the model too: the agent stamps the model's own name on the responses it keeps.
    return FunctionModel(fn, model_name="claude-haiku-5-5")


def agent_with(model: FunctionModel, settings: Settings, *capabilities: object) -> Agent[None, str]:
    agent: Agent[None, str] = Agent(model, capabilities=[build_cost_hooks(settings), *capabilities])  # type: ignore[list-item]

    @agent.tool_plain
    def lookup(q: str) -> str:
        return "found"

    return agent


async def test_both_count_the_same_dollars(settings: Settings) -> None:
    counter = SpendLimits[None](budgets=[Budget(window="total", name="all")])  # no ceiling: a counter
    await agent_with(haiku_like(2), settings, counter).run("go")
    [status] = await counter.status()
    assert status.spent.requests == 3
    assert status.spent.usd == total_spend(path=settings.costs_path) == Decimal("0.00045")


async def test_spend_limits_stops_mid_run_where_cost_hooks_cannot(settings: Settings) -> None:
    # CostHooks checks the project total before a run starts: $0 spent, so this run is let in and
    # nothing stops it later. SpendLimits checks before every request.
    brake = SpendLimits[None](budgets=[Budget(usd=Decimal("0.0002"), window="run")])
    with pytest.raises(SpendLimitExceeded):
        await agent_with(haiku_like(5), settings, brake).run("go")
    # Request 1 ($0.00015) fits, request 2 crosses the line and completes (it's a brake, not a
    # guarantee), request 3 is refused before it is sent.
    lines = settings.costs_path.read_text().splitlines()
    assert len(lines) == 2


async def test_neither_sees_a_harness_agents_own_requests(settings: Settings) -> None:
    # The judge runs its own internal agent. Its tokens join the run's usage, but its requests never
    # pass through the host agent's capabilities: neither meter records them (hence CostRecordingModel).
    judge_calls: list[str] = []

    def judge(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        judge_calls.append("judged")
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"response": "no"})],
            usage=USAGE, model_name="claude-haiku-5-5", provider_name="anthropic",
        )  # fmt: skip

    counter = SpendLimits[None](budgets=[Budget(window="total", name="all")])
    result = await agent_with(
        haiku_like(1),
        settings,
        counter,
        ToolCallJudge(FunctionModel(judge), question="Risky?", tools=["lookup"]),
    ).run("go")
    [status] = await counter.status()
    assert judge_calls == ["judged"] and result.usage.requests == 3  # 2 host + 1 judge in run usage
    assert status.spent.requests == 2
    assert [json.loads(line)["label"] for line in settings.costs_path.read_text().splitlines()] == [
        "agent"
    ] * 2
