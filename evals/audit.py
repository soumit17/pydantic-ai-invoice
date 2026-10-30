"""Dataset 2, audit detection (spec §17.2). LIVE: about $0.03 on Haiku, $0.50 on Sonnet.

Inputs are the ground-truth invoices, so extraction errors can't leak into the audit scores. Each
case runs the pipeline's decision path: deterministic checks (seed ledger), the auditor with
escalation, merge, policy.

    uv run python -m evals.audit --limit 5
    uv run python -m evals.audit           # all 36 (acceptance: recall ≥ 0.85, precision ≥ 0.9)
    uv run python -m evals.audit --final
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, MaxToolCalls, ToolCorrectness, TrajectoryMatch

from evals.common import evaluate, ground_truths, parse_args, setup
from evals.evaluators import (
    AuditResult,
    AuditSummary,
    CaseMeta,
    DecisionMatch,
    DocInput,
    FindingPrecision,
    FindingRecall,
    SpansCaptured,
)
from invoiceiq.agents.auditor import AuditorAgent, audit_deps, build_auditor, merge_findings, run_audit
from invoiceiq.config import Settings
from invoiceiq.models import GroundTruth, RawInvoice
from invoiceiq.pipeline.checks import run_deterministic_checks
from invoiceiq.policy import to_inr

DUPLICATES = {"exact_duplicate", "fuzzy_duplicate"}


def case_evaluators(t: GroundTruth) -> list[Evaluator[DocInput, AuditResult, CaseMeta]]:
    """Agentic checks where the right investigation is known (read from the run's tool spans)."""
    kinds = {f.kind for f in t.expected_findings}
    evaluators: list[Evaluator[DocInput, AuditResult, CaseMeta]] = []
    if "price_drift" in kinds:
        # Price drift is a claim about the PO price: the auditor should have looked the PO up.
        evaluators.append(ToolCorrectness(expected_tools=["get_po"], allow_extra=True))
    if kinds & DUPLICATES:
        # Identify the vendor before searching its postings for the duplicate.
        evaluators.append(
            TrajectoryMatch(expected_trajectory=["get_vendor", "search_posted"], order="in_order")
        )
    return evaluators


def trajectory(messages: list[ModelMessage]) -> list[str]:
    return [
        p.tool_name
        for m in messages
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, ToolCallPart) and p.tool_name != "final_result"
    ]


def build_dataset(truths: list[GroundTruth]) -> Dataset[DocInput, AuditResult, CaseMeta]:
    return Dataset[DocInput, AuditResult, CaseMeta](
        name="audit",
        cases=[
            Case(
                name=t.file_id,
                inputs=DocInput(file_id=t.file_id, file_name=t.file_name),
                expected_output=AuditResult(
                    flagged=sorted({f.kind for f in t.expected_findings}), decision=t.expected_decision
                ),
                metadata=CaseMeta(category=t.category_name),
                evaluators=case_evaluators(t),
            )
            for t in truths
            if isinstance(t.expected, RawInvoice)
        ],
        evaluators=[
            FindingRecall(),
            FindingPrecision(),
            DecisionMatch(),
            MaxToolCalls(max_calls=8),
            SpansCaptured(),
        ],
        report_evaluators=[AuditSummary()],
    )


def audit_task(
    agent: AuditorAgent, settings: Settings, truths: list[GroundTruth]
) -> Callable[[DocInput], Awaitable[AuditResult]]:
    invoices = {t.file_id: t.expected for t in truths}
    deps = audit_deps(settings, seed=True)  # the clean seed ledger, as in M3/M6

    async def audit(doc: DocInput) -> AuditResult:
        invoice = invoices[doc.file_id]
        assert isinstance(invoice, RawInvoice)
        rules = await run_deterministic_checks(
            invoice, ledger=deps.ledger, policy=deps.policy, today=deps.today
        )
        outcome = await run_audit(
            agent,
            invoice,
            rules,
            deps,
            escalate=settings.audit_escalation,
            metadata={"invoice_id": doc.file_id},
        )
        merged = merge_findings(rules, outcome.report.findings)
        decision = deps.policy.decide(merged, total_inr=to_inr(invoice.total, invoice.currency))
        return AuditResult(
            # info findings are observations, not flags
            flagged=sorted({f.kind for f in merged if f.severity != "info"}),
            decision=decision.outcome,
            llm_findings=[f"{f.severity} {f.kind}" for f in outcome.report.findings],
            narrative=outcome.report.narrative,
            escalation=outcome.escalation,
            trajectory=trajectory(outcome.run.all_messages()),
        )

    return audit


async def main() -> None:
    args = parse_args(__doc__ or "")
    settings, model, spent = setup(args)
    truths = ground_truths(settings, args)
    task = audit_task(build_auditor(settings, model=model), settings, truths)
    await evaluate(build_dataset(truths), task, settings=settings, model=model, spent_before=spent, args=args)


if __name__ == "__main__":
    asyncio.run(main())
