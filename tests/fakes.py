"""FunctionModel-backed stand-ins for the three agents, for $0 pipeline and approval tests."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from invoiceiq.agents.auditor import AuditorAgent, build_auditor
from invoiceiq.agents.extractor import FAILURE_TOOL, INVOICE_TOOL, ExtractorAgent, build_extractor
from invoiceiq.agents.poster import RECEIPT_TOOL, PosterAgent, build_poster
from invoiceiq.config import Settings
from invoiceiq.models import ExtractionFailure, GroundTruth, RawInvoice

AS_OF = date(2026, 10, 29)


def runtime_settings(dataset: Path, root: Path) -> Settings:
    """Settings for a fresh, isolated runtime: the generated seed ledger copied into `root`."""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(dataset / "ledger.sqlite", data / "ledger.sqlite")
    return Settings(_env_file=None, data_dir=data, runs_dir=root / "runs", as_of_date=AS_OF)  # pyright: ignore[reportCallIssue]


def truth(dataset: Path, file_id: str) -> GroundTruth:
    return GroundTruth.model_validate_json((dataset / "ground_truth" / f"{file_id}.json").read_text())


def fake_extractor(settings: Settings, expected: RawInvoice | ExtractionFailure) -> ExtractorAgent:
    """Answers every document with the ground truth: isolates the graph from extraction quality."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool = INVOICE_TOOL if isinstance(expected, RawInvoice) else FAILURE_TOOL
        args = json.loads(expected.model_dump_json(exclude={"kind"}))
        # Report real-world usage (from the M2 live runs). Without it, FunctionModel estimates tokens
        # from the raw bytes of a scan (~1.2M "tokens") and trips the extractor's UsageLimits.
        return ModelResponse(
            parts=[ToolCallPart(tool, args)], usage=RequestUsage(input_tokens=4500, output_tokens=900)
        )

    return build_extractor(settings, model=FunctionModel(fn))


def fake_auditor(settings: Settings) -> AuditorAgent:
    """Adds no findings, so decisions come purely from the deterministic checks and the policy."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        report = {"findings": [], "narrative": "Checked.", "suggested_decision": "approve"}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, report)])

    return build_auditor(settings, model=FunctionModel(fn))


@dataclass
class PosterCalls:
    requests: int = 0
    seen_outcomes: list[str] = field(default_factory=lambda: [])


def fake_poster(settings: Settings, calls: PosterCalls | None = None) -> PosterAgent:
    """Request 1: call post_to_ledger with the arguments from the prompt (pauses for approval).
    Request 2: return the receipt, or a sentence if the call was denied."""
    calls = calls or PosterCalls()

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.requests += 1
        last = messages[-1]
        returns = (
            [p for p in last.parts if isinstance(p, ToolReturnPart) and p.tool_name == "post_to_ledger"]
            if isinstance(last, ModelRequest)
            else []
        )
        if not returns:
            prompt = next(p.content for p in messages[0].parts if isinstance(p, UserPromptPart))
            assert isinstance(prompt, str)
            args = json.loads(prompt.split("Arguments: ", 1)[1])
            return ModelResponse(parts=[ToolCallPart("post_to_ledger", args)])
        ret = returns[0]
        calls.seen_outcomes.append(ret.outcome)
        if ret.outcome == "success":
            return ModelResponse(parts=[ToolCallPart(RECEIPT_TOOL, ret.model_response_object())])
        return ModelResponse(parts=[TextPart(f"Not posted: {ret.content}")])

    return build_poster(settings, model=FunctionModel(fn))
