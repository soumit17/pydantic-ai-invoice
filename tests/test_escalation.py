"""Thinking escalation: re-run the auditor with per-run Thinking() when it disagrees with the rules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.settings import ModelSettings

from invoiceiq.agents.auditor import (
    ESCALATION_MAX_TOKENS,
    AuditDeps,
    build_auditor,
    disagreements,
    escalation_reason,
    run_audit,
)
from invoiceiq.models import AuditFinding, AuditReport, Finding, RawInvoice
from invoiceiq.pipeline.graph import InvoiceSource, pipeline_deps, run_pipeline
from invoiceiq.policy import Policy
from invoiceiq.repos.ledger import open_ledger
from tests.fakes import AS_OF, fake_extractor, fake_poster, runtime_settings, truth

FALSE_ALARM = {
    "kind": "gst_type_mismatch", "severity": "warning",
    "message": "IGST charged; the supplier is inter-state, so this is correct.",
}  # fmt: skip


def finding(kind: str, severity: str = "warning") -> AuditFinding:
    return AuditFinding(kind=kind, severity=severity, message="m")  # pyright: ignore[reportArgumentType]


def rule(kind: str) -> Finding:
    return Finding(kind=kind, severity="warning", message="m", source="deterministic")  # pyright: ignore[reportArgumentType]


def report(*findings: AuditFinding) -> AuditReport:
    return AuditReport(findings=list(findings), narrative="n", suggested_decision="approve")


# --- the rule -----------------------------------------------------------------------------------


def test_a_new_warning_the_rules_did_not_raise_is_a_disagreement() -> None:
    assert [f.kind for f in disagreements(report(finding("gst_type_mismatch")), [])] == ["gst_type_mismatch"]


def test_info_findings_and_kinds_the_rules_already_raised_are_not() -> None:
    rules = [rule("price_drift")]
    assert disagreements(report(finding("price_drift"), finding("stale_date", "info")), rules) == []


def test_low_extraction_confidence_also_escalates() -> None:
    assert escalation_reason(report(), [], confidence=0.95) is None
    assert escalation_reason(report(), [], confidence=0.6) == "extraction confidence 0.60 < 0.8"


# --- run_audit ----------------------------------------------------------------------------------


class Spy(FunctionModel):
    """Records the merged settings each request is sent with (thinking, max_tokens)."""

    sent: list[dict[str, Any]]

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        params: ModelRequestParameters,
    ) -> ModelResponse:
        self.sent.append(dict(model_settings or {}))
        return await super().request(messages, model_settings, params)


def auditor_answering(*answers: list[dict[str, Any]]) -> Spy:
    """Run n answers with answers[n]'s findings."""
    runs: list[int] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:  # a new run starts with just the prompt
            runs.append(1)
        body = {"findings": answers[len(runs) - 1], "narrative": "n", "suggested_decision": "approve"}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, body)])

    spy = Spy(fn)
    spy.sent = []
    return spy


async def _audit(dataset: Path, tmp_path: Path, model: Spy, *, escalate: bool = True):  # type: ignore[no-untyped-def]
    settings = runtime_settings(dataset, tmp_path)
    gt = truth(dataset, "inv-014")  # the live run's reproducible false alarm
    assert isinstance(gt.expected, RawInvoice)
    deps = AuditDeps(ledger=open_ledger(settings), policy=Policy(), today=AS_OF)
    return await run_audit(build_auditor(settings, model=model), gt.expected, [], deps, escalate=escalate)


async def test_disagreement_reruns_once_with_thinking_and_room_for_it(dataset: Path, tmp_path: Path) -> None:
    model = auditor_answering([FALSE_ALARM], [])  # thinking it over, the auditor withdraws it
    outcome = await _audit(dataset, tmp_path, model)
    assert outcome.escalation == "auditor disagrees with the rules: warning gst_type_mismatch"
    assert outcome.report.findings == [] and outcome.first_report is not None
    assert [(s.get("thinking"), s.get("max_tokens")) for s in model.sent] == [
        (False, 2048),  # the normal run: thinking off (the agent's own Thinking(effort=False))
        ("medium", ESCALATION_MAX_TOKENS),  # the per-run Thinking capability wins, with headroom
    ]


async def test_a_prebuilt_escalation_agent_replaces_the_per_run_capability(
    dataset: Path, tmp_path: Path
) -> None:
    # The durable batch's path (D22): DBOS rejects capabilities added per run, so escalation runs a
    # second agent that was built with thinking on, and its own max_tokens headroom.
    settings = runtime_settings(dataset, tmp_path)
    gt = truth(dataset, "inv-014")
    assert isinstance(gt.expected, RawInvoice)
    first, second = auditor_answering([FALSE_ALARM]), auditor_answering([])
    escalation = build_auditor(settings, model=second, thinking="medium")
    assert escalation.name == "auditor_thinking"  # DBOS step names derive from it: must be distinct
    deps = AuditDeps(ledger=open_ledger(settings), policy=Policy(), today=AS_OF)
    auditor = build_auditor(settings, model=first)
    outcome = await run_audit(auditor, gt.expected, [], deps, escalate=True, escalation_agent=escalation)
    assert outcome.escalation is not None and outcome.report.findings == []
    assert [(s.get("thinking"), s.get("max_tokens")) for s in first.sent] == [(False, 2048)]
    expected = [("medium", ESCALATION_MAX_TOKENS)]
    assert [(s.get("thinking"), s.get("max_tokens")) for s in second.sent] == expected


async def test_agreement_runs_once(dataset: Path, tmp_path: Path) -> None:
    model = auditor_answering([])
    outcome = await _audit(dataset, tmp_path, model)
    assert outcome.escalation is None and len(model.sent) == 1


async def test_escalation_can_be_switched_off(dataset: Path, tmp_path: Path) -> None:
    model = auditor_answering([FALSE_ALARM], [])
    outcome = await _audit(dataset, tmp_path, model, escalate=False)
    assert outcome.escalation is None and [f.kind for f in outcome.report.findings] == ["gst_type_mismatch"]


async def test_escalation_in_the_graph_turns_inv_014_back_into_an_auto_approval(
    dataset: Path, tmp_path: Path
) -> None:
    settings = runtime_settings(dataset, tmp_path)
    gt = truth(dataset, "inv-014")
    model = auditor_answering([FALSE_ALARM], [])
    deps = await pipeline_deps(
        settings, extractor=fake_extractor(settings, gt.expected),
        auditor=build_auditor(settings, model=model), poster=fake_poster(settings),
    )  # fmt: skip
    record = await run_pipeline(InvoiceSource("inv-014", dataset / "invoices" / gt.file_name), deps)
    assert record.status == "posted" and record.findings == []
    assert record.audit_escalation is not None and "gst_type_mismatch" in record.audit_escalation
