"""FunctionModel-backed stand-ins for the three agents, for $0 pipeline and approval tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic_ai import BinaryContent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.usage import RequestUsage

from invoiceiq.agents.ask_ap import AskAgent, build_ask_ap
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
    return Settings(  # pyright: ignore[reportCallIssue]
        _env_file=None, data_dir=data, runs_dir=root / "runs", as_of_date=AS_OF, online_eval_rate=0
    )


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

    async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        # Used when the run streams (an event_stream_handler is attached): the same answer, in chunks.
        tool = INVOICE_TOOL if isinstance(expected, RawInvoice) else FAILURE_TOOL
        payload = expected.model_dump_json(exclude={"kind"})
        for i in range(0, len(payload), 150):
            yield {0: DeltaToolCall(name=tool if i == 0 else None, json_args=payload[i : i + 150])}

    return build_extractor(settings, model=FunctionModel(fn, stream_function=stream_fn))


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


def fake_guard(answer: Callable[[str], str], prompts: list[str] | None = None) -> FunctionModel:
    """The ApprovalReviewer's / ToolCallJudge's model: answer(instructions + prompt) returns 'yes',
    'no' or 'unsure' (the instructions carry the question, which tells the two apart). Their
    agents' output type is that Literal, as an output tool."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = next(p.content for p in messages[0].parts if isinstance(p, UserPromptPart))
        assert isinstance(prompt, str)
        prompt = f"{info.instructions}\n{prompt}"
        if prompts is not None:
            prompts.append(prompt)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"response": answer(prompt)})])

    return FunctionModel(fn, model_name="fake-guard")


def fake_poster(
    settings: Settings, calls: PosterCalls | None = None, *, guards: Model | Literal[False] = False
) -> PosterAgent:
    """Request 1: call post_to_ledger with the arguments from the prompt (pauses for approval).
    Request 2: return the receipt, or a sentence if the call was denied.
    guards: a fake_guard model for the reviewer and the amount judge; off by default."""
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
        blocked = "tool_call_judge" in (ret.metadata or {})  # ToolCallJudge: a return, not a denial
        if ret.outcome == "success" and not blocked:
            return ModelResponse(parts=[ToolCallPart(RECEIPT_TOOL, ret.model_response_object())])
        return ModelResponse(parts=[TextPart(f"Not posted: {ret.content}")])

    return build_poster(settings, model=FunctionModel(fn), guards=guards)


def fake_ask_ap(settings: Settings) -> AskAgent:
    """Replies with every user message it can see in the conversation, proving memory."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        asked = [
            str(p.content)
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, UserPromptPart)
        ]
        return ModelResponse(parts=[TextPart(" | ".join(asked))])

    return build_ask_ap(settings, model=FunctionModel(fn))


# --- batch fakes (M7): one agent, many invoices --------------------------------------------------


def _log_call(log: Path | None, agent: str, invoice: str) -> None:
    if log is not None:
        with log.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps({"agent": agent, "invoice": invoice, "pid": os.getpid(), "t": time.time()}) + "\n"
            )


def fake_router_extractor(
    settings: Settings, dataset: Path, *, durable: bool = False, delay: float = 0.0, log: Path | None = None
) -> ExtractorAgent:
    """Recognises which invoice it is reading (by file hash, or invoice number for email) and answers
    with that invoice's ground truth. Optionally slow, and logging every call."""
    by_hash: dict[str, GroundTruth] = {}
    truths = [
        GroundTruth.model_validate_json(p.read_text())
        for p in sorted((dataset / "ground_truth").glob("*.json"))
    ]
    for gt in truths:
        by_hash[hashlib.sha256((dataset / "invoices" / gt.file_name).read_bytes()).hexdigest()] = gt

    def identify(messages: list[ModelMessage]) -> GroundTruth:
        first = messages[0]
        assert isinstance(first, ModelRequest)
        for part in first.parts:
            if isinstance(part, UserPromptPart) and not isinstance(part.content, str):
                for item in part.content:
                    if isinstance(item, BinaryContent):
                        return by_hash[hashlib.sha256(item.data).hexdigest()]
                    if isinstance(item, str):
                        for gt in truths:
                            if (
                                isinstance(gt.expected, RawInvoice)
                                and f"Invoice No: {gt.expected.invoice_no}" in item
                            ):
                                return gt
        raise AssertionError("unknown document")

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        gt = identify(messages)
        _log_call(log, "extractor", gt.file_id)
        await asyncio.sleep(delay)
        tool = INVOICE_TOOL if isinstance(gt.expected, RawInvoice) else FAILURE_TOOL
        args = json.loads(gt.expected.model_dump_json(exclude={"kind"}))
        return ModelResponse(
            parts=[ToolCallPart(tool, args)], usage=RequestUsage(input_tokens=4500, output_tokens=900)
        )

    return build_extractor(settings, model=FunctionModel(fn, model_name="fake-extractor"), durable=durable)


# The batch auditor's one false alarm: without thinking it flags inv-014's (correct) IGST, which
# escalates the audit; with thinking it withdraws it. So every batch run exercises escalation.
FALSE_ALARM_ON = "inv-014"


def fake_batch_auditor(
    settings: Settings,
    dataset: Path,
    *,
    durable: bool = False,
    thinking: Literal["medium"] | Literal[False] = False,
    delay: float = 0.0,
    log: Path | None = None,
) -> AuditorAgent:
    """Adds no findings, except the FALSE_ALARM_ON warning when thinking is off; logs which invoice
    it audited (read from the invoice JSON in the prompt)."""
    ids = {
        (gt.expected.supplier.name, gt.expected.invoice_no): gt.file_id
        for p in sorted((dataset / "ground_truth").glob("*.json"))
        if isinstance((gt := GroundTruth.model_validate_json(p.read_text())).expected, RawInvoice)
    }

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = next(p.content for p in messages[0].parts if isinstance(p, UserPromptPart))  # type: ignore[union-attr]
        inv = json.loads(str(prompt).split("Invoice:\n", 1)[1].split("\n\nTotal:", 1)[0])
        invoice_id = ids.get((inv["supplier"]["name"], inv["invoice_no"]), "?")
        thought = thinking is not False  # FunctionModel's AgentInfo doesn't carry the thinking setting
        _log_call(log, "auditor_thinking" if thought else "auditor", invoice_id)
        await asyncio.sleep(delay)
        findings: list[dict[str, str]] = []
        if invoice_id == FALSE_ALARM_ON and not thought:
            findings = [{"kind": "gst_type_mismatch", "severity": "warning", "message": "IGST? (wrongly)"}]
        report = {"findings": findings, "narrative": "Checked.", "suggested_decision": "approve"}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, report)])

    return build_auditor(
        settings, model=FunctionModel(fn, model_name="fake-auditor"), durable=durable, thinking=thinking
    )
