"""Auditor tests: FunctionModel scripts each step; the real ledger tools run against the mock ledger."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import capture_run_messages
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from invoiceiq.agents.auditor import AuditDeps, audit_prompt, build_auditor, merge_findings
from invoiceiq.config import Settings
from invoiceiq.models import Finding, GroundTruth, RawInvoice
from invoiceiq.policy import Policy
from invoiceiq.repos.ledger import LedgerRepo

Step = Callable[[AgentInfo], ModelResponse]


def truth(dataset: Path, file_id: str) -> RawInvoice:
    gt = GroundTruth.model_validate_json((dataset / "ground_truth" / f"{file_id}.json").read_text())
    assert isinstance(gt.expected, RawInvoice)
    return gt.expected


def deps(dataset: Path) -> AuditDeps:
    return AuditDeps(ledger=LedgerRepo(dataset / "ledger.sqlite"), policy=Policy(), today=date(2026, 10, 29))


def call(tool: str, **args: Any) -> Step:
    return lambda info: ModelResponse(parts=[ToolCallPart(tool, args)])


def report(*findings: dict[str, Any], decision: str = "approve") -> Step:
    def step(info: AgentInfo) -> ModelResponse:
        out = {"findings": list(findings), "narrative": "Looks fine.", "suggested_decision": decision}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, out)])

    return step


def scripted(*steps: Step) -> tuple[FunctionModel, list[AgentInfo]]:
    seen: list[AgentInfo] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return steps[len(seen) - 1](info)

    return FunctionModel(fn), seen


def tool_calls(messages: list[ModelMessage]) -> list[str]:
    return [
        p.tool_name
        for m in messages
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, ToolCallPart)
    ]


def tool_names(info: AgentInfo) -> set[str]:
    return {t.name for t in info.function_tools}


# --- Tool order and results ---------------------------------------------------------------------


async def test_checks_vendor_and_po_before_reporting(settings: Settings, dataset: Path) -> None:
    inv = truth(dataset, "inv-024")  # price drift, has a PO
    model, _ = scripted(
        call("get_vendor", gstin=inv.supplier.gstin),
        call("get_po", po_number=inv.po_number),
        report(decision="needs_approval"),
    )
    agent = build_auditor(settings, model=model)
    with capture_run_messages() as messages:
        await agent.run(audit_prompt(inv, [], Policy()), deps=deps(dataset))
    assert tool_calls(messages)[:2] == ["get_vendor", "get_po"]  # the order the spec asks for
    returns = {p.tool_name: p.content for m in messages if isinstance(m, ModelRequest)
               for p in m.parts if isinstance(p, ToolReturnPart)}  # fmt: skip
    assert returns["get_vendor"].name == inv.supplier.name
    assert returns["get_po"].po_number == inv.po_number and returns["get_po"].lines


async def test_bad_po_number_gets_a_helpful_retry(settings: Settings, dataset: Path) -> None:
    inv = truth(dataset, "inv-024")
    model, _ = scripted(call("get_po", po_number="2026/7"), call("get_po", po_number=inv.po_number), report())
    agent = build_auditor(settings, model=model)
    with capture_run_messages() as messages:
        await agent.run(audit_prompt(inv, [], Policy()), deps=deps(dataset))
    # V2: the retry is the tool's own return part, marked outcome="retried" with RetryInfo attached.
    retried = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts
               if isinstance(p, ToolReturnPart) and p.outcome == "retried"]  # fmt: skip
    assert len(retried) == 1 and retried[0].tool_name == "get_po"
    assert "PO-YYYY-NNNN" in str(retried[0].content)


async def test_search_posted_finds_reformatted_numbers(dataset: Path) -> None:
    inv = truth(dataset, "inv-022")  # fuzzy duplicate: posted with '-' instead of '/'
    hits = await LedgerRepo(dataset / "ledger.sqlite").search_posted(inv.invoice_no)
    assert any(h.invoice_no == inv.invoice_no.replace("/", "-") for h in hits)


# --- Deferred FX capability ---------------------------------------------------------------------


async def test_fx_is_hidden_until_loaded(settings: Settings, dataset: Path) -> None:
    inv = truth(dataset, "inv-038")  # USD import
    model, seen = scripted(
        call("load_capability", id="fx"),
        call("convert_to_inr", amount=str(inv.total), currency="USD", on_date=inv.invoice_date.isoformat()),
        report(decision="needs_approval"),
    )
    agent = build_auditor(settings, model=model)
    with capture_run_messages() as messages:
        await agent.run(audit_prompt(inv, [], Policy()), deps=deps(dataset))
    assert "convert_to_inr" not in tool_names(seen[0]) and "load_capability" in tool_names(seen[0])
    assert "fx: Currency conversion" in (seen[0].instructions or "")  # the one-line catalog entry
    assert "convert_to_inr" in tool_names(seen[1])  # visible only after load_capability
    converted = [p.content for m in messages if isinstance(m, ModelRequest) for p in m.parts
                 if isinstance(p, ToolReturnPart) and p.tool_name == "convert_to_inr"]  # fmt: skip
    assert converted == [inv.total * 84]


async def test_inr_invoice_never_sees_the_fx_tool(settings: Settings, dataset: Path) -> None:
    inv = truth(dataset, "inv-001")
    model, seen = scripted(call("get_vendor", gstin=inv.supplier.gstin), report())
    await build_auditor(settings, model=model).run(audit_prompt(inv, [], Policy()), deps=deps(dataset))
    assert all("convert_to_inr" not in tool_names(info) for info in seen)


# --- Instructions and output --------------------------------------------------------------------


async def test_instructions_combine_agent_ledger_and_policy(settings: Settings, dataset: Path) -> None:
    model, seen = scripted(report())
    await build_auditor(settings, model=model).run("x", deps=deps(dataset))
    text = seen[0].instructions or ""
    assert "Do not repeat them" in text  # the agent's own instructions
    assert "check vendor status and the PO lines" in text  # travelled in with LedgerCapability
    assert "Auto-approve limit: ₹50000" in text and "older than 180 days" in text  # dynamic, from deps


async def test_findings_from_the_model_are_marked_llm(settings: Settings, dataset: Path) -> None:
    claimed = {"kind": "price_drift", "severity": "info", "message": "exactly 5%", "source": "deterministic"}
    model, _ = scripted(report(claimed))
    result = await build_auditor(settings, model=model).run("x", deps=deps(dataset))
    assert [f.source for f in result.output.findings] == ["llm"]


def f(kind: str, source: str = "deterministic") -> Finding:
    return Finding(kind=kind, severity="warning", message="m", source=source)  # pyright: ignore[reportArgumentType]


def test_merge_keeps_rules_authoritative() -> None:
    merged = merge_findings([f("price_drift")], [f("price_drift", "llm"), f("stale_date", "llm")])
    assert [(x.kind, x.source) for x in merged] == [("price_drift", "deterministic"), ("stale_date", "llm")]


def test_audit_prompt_lists_findings() -> None:
    from tests.test_m1_domain import invoice

    prompt = audit_prompt(invoice(), [f("price_drift")], Policy())
    assert "Deterministic findings (1)" in prompt and '"price_drift"' in prompt and "confidence" not in prompt
    assert "within the ₹50000 auto-approve limit" in prompt  # computed in code for INR


def test_audit_prompt_leaves_foreign_totals_to_the_fx_tool() -> None:
    from tests.test_m1_domain import invoice

    prompt = audit_prompt(invoice(currency="USD", supplier_gstin=None), [], Policy())
    assert "Convert it to INR" in prompt and "auto-approve limit." not in prompt


@pytest.mark.parametrize("bad", ["V7", "vendor-7"])
async def test_vendor_history_validates_ids(settings: Settings, dataset: Path, bad: str) -> None:
    model, _ = scripted(
        call("vendor_history", vendor_id=bad), call("vendor_history", vendor_id="V001"), report()
    )
    with capture_run_messages() as messages:
        await build_auditor(settings, model=model).run("x", deps=deps(dataset))
    assert tool_calls(messages).count("vendor_history") == 2  # retried once with a valid id
