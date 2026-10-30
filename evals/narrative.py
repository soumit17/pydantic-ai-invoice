"""Dataset 3, narrative quality (spec §17.3): an LLM judge on 10 cases. LIVE: about $0.01 on Haiku.

The auditor writes the narrative an approver reads; LLMJudge grades it against a rubric. The judge is
Haiku even with --final (only the auditor changes). A deterministic sentence count runs alongside,
so you can see where the judge and a plain rule disagree.

    uv run python -m evals.narrative
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from pydantic_ai.settings import ModelSettings
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge, ReportEvaluator
from pydantic_evals.evaluators.report_evaluator import ReportEvaluatorContext
from pydantic_evals.reporting.analyses import ReportAnalysis, ScalarResult

from evals.common import evaluate, ground_truths, parse_args, setup
from evals.evaluators import CaseMeta, DocInput
from invoiceiq.agents.auditor import audit_deps, build_auditor, merge_findings, run_audit
from invoiceiq.capabilities.guards import guard_model
from invoiceiq.models import RawInvoice
from invoiceiq.pipeline.checks import run_deterministic_checks
from invoiceiq.policy import to_inr

# Clean, high-value, both duplicates, drift, blocked/unknown vendor, GST, math + stale, FX.
CASES = [
    "inv-001",
    "inv-017",
    "inv-020",
    "inv-022",
    "inv-024",
    "inv-028",
    "inv-029",
    "inv-030",
    "inv-033",
    "inv-038",
]

RUBRIC = """\
The output is an invoice audit: the invoice facts, the findings (with severity), the decision, and a
narrative written for the person who approves the payment. Pass only if the narrative:
1. mentions every critical and warning finding listed (info findings are optional);
2. states nothing that the facts and findings don't support (no invented numbers, vendors or checks);
3. is at most 4 sentences;
4. is actionable: the approver can tell what to do or what to check next."""


class NarrativeCase(BaseModel):
    """Everything the judge needs to check the narrative against, and nothing else."""

    vendor: str
    invoice_no: str
    total: str
    po_number: str | None
    findings: list[str]
    decision: str
    narrative: str


@dataclass
class AtMostFourSentences(Evaluator[DocInput, NarrativeCase, CaseMeta]):
    """Rubric item 3 as a plain rule, to compare with the judge's verdict."""

    def evaluate(self, ctx: EvaluatorContext[DocInput, NarrativeCase, CaseMeta]) -> bool:
        return len([s for s in re.split(r"(?<=[.!?])\s+", ctx.output.narrative.strip()) if s]) <= 4


@dataclass
class NarrativeSummary(ReportEvaluator[DocInput, NarrativeCase, CaseMeta]):
    def evaluate(
        self, ctx: ReportEvaluatorContext[DocInput, NarrativeCase, CaseMeta]
    ) -> list[ReportAnalysis]:
        def rate(name: str) -> float:
            results: list[Any] = [c.assertions[name].value for c in ctx.report.cases if name in c.assertions]
            return sum(map(bool, results)) / len(results) if results else 0.0

        return [
            ScalarResult(title="narrative_pass_rate", value=rate("LLMJudge")),
            ScalarResult(title="four_sentences_rate", value=rate("AtMostFourSentences")),
        ]


async def main() -> None:
    args = parse_args(__doc__ or "")
    settings, model, spent = setup(args)
    if not args.ids:
        args.ids = CASES
    truths = {t.file_id: t for t in ground_truths(settings, args) if isinstance(t.expected, RawInvoice)}
    judge = LLMJudge(
        rubric=RUBRIC,
        # Haiku, wrapped so its requests reach the cost ledger; the judge's own agent is internal (D23).
        model=guard_model(settings, "narrative_judge"),
        model_settings=ModelSettings(max_tokens=600, thinking=False),
    )
    dataset = Dataset[DocInput, NarrativeCase, CaseMeta](
        name="narrative",
        cases=[
            Case(
                name=t.file_id,
                inputs=DocInput(file_id=t.file_id, file_name=t.file_name),
                metadata=CaseMeta(category=t.category_name),
            )
            for t in truths.values()
        ],
        evaluators=[judge, AtMostFourSentences()],
        report_evaluators=[NarrativeSummary()],
    )
    agent = build_auditor(settings, model=model)
    deps = audit_deps(settings, seed=True)

    async def audit(doc: DocInput) -> NarrativeCase:
        invoice = truths[doc.file_id].expected
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
        return NarrativeCase(
            vendor=invoice.supplier.name,
            invoice_no=invoice.invoice_no,
            total=f"{invoice.currency} {invoice.total}",
            po_number=invoice.po_number,
            findings=[f"{f.severity} {f.kind}: {f.message}" for f in merged],
            decision=decision.outcome,
            narrative=outcome.report.narrative,
        )

    await evaluate(dataset, audit, settings=settings, model=model, spent_before=spent, args=args)


if __name__ == "__main__":
    asyncio.run(main())
