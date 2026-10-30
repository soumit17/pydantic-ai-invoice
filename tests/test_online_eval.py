"""OnlineEvaluation on the auditor (M8, spec §17.4): results arrive as gen_ai.evaluation.result events."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import logfire
import pytest
from logfire.testing import CaptureLogfire  # capfire: the logfire pytest plugin's fixture
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals.online import wait_for_evaluations

from invoiceiq.agents.auditor import AuditDeps, build_auditor
from invoiceiq.policy import Policy
from invoiceiq.repos.ledger import open_ledger
from tests.fakes import runtime_settings


@pytest.fixture
def instrumented() -> Iterator[None]:
    # The tool-call evaluator reads spans, as in production (telemetry.py instruments every agent).
    logfire.instrument_pydantic_ai()
    yield
    Agent.instrument_all(False)


def auditor_model(narrative: str) -> FunctionModel:
    """Calls get_vendor once, then reports with the given narrative."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("get_vendor", {"name": "Acme"})])
        report = {"findings": [], "narrative": narrative, "suggested_decision": "approve"}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, report)])

    return FunctionModel(fn)


def evaluation_events(capfire: CaptureLogfire) -> dict[str, Any]:
    """The gen_ai.evaluation.result log events, by evaluator name (the exporter's dicts don't carry
    the event name itself, so match on its gen_ai.evaluation.* attributes)."""
    logs = capfire.log_exporter.exported_logs_as_dicts()
    events = [r["attributes"] for r in logs if "gen_ai.evaluation.name" in r["attributes"]]
    return {e["gen_ai.evaluation.name"]: e for e in events}


@pytest.mark.parametrize(
    ("narrative", "narrative_ok"), [("All checks passed; vendor and PO verified.", True), ("ok", False)]
)
async def test_sampled_runs_emit_evaluation_events(
    dataset: Path,
    tmp_path: Path,
    capfire: CaptureLogfire,
    instrumented: None,
    narrative: str,
    narrative_ok: bool,
) -> None:
    settings = runtime_settings(dataset, tmp_path).model_copy(update={"online_eval_rate": 1.0})
    agent = build_auditor(settings, model=auditor_model(narrative))
    deps = AuditDeps(ledger=open_ledger(settings), policy=Policy(), today=date(2026, 10, 29))
    await agent.run("audit this", deps=deps)
    await wait_for_evaluations()  # evaluators run in the background; the run didn't wait for them
    results = evaluation_events(capfire)
    assert set(results) == {"MaxToolCalls", "NarrativeNotEmpty"}
    assert results["NarrativeNotEmpty"]["gen_ai.evaluation.score.value"] == (1.0 if narrative_ok else 0.0)
    assert results["MaxToolCalls"]["gen_ai.evaluation.score.value"] == 1.0  # 1 call ≤ 8


async def test_rate_zero_attaches_nothing(dataset: Path, tmp_path: Path) -> None:
    agent = build_auditor(runtime_settings(dataset, tmp_path), model=auditor_model("x"))
    assert not any(type(c).__name__ == "OnlineEvaluation" for c in agent.root_capability.capabilities)
