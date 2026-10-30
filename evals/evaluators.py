"""Custom evaluators for the three eval datasets (spec §17).

Case-level `Evaluator`s score one case; a `ReportEvaluator` sees the whole report and produces the
headline metrics (micro-averaged, so a 9-line invoice weighs more than a 1-line one). The headline
`ScalarResult`s are what `evals.compare` gates on.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher

from pydantic import BaseModel
from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext, ReportEvaluator
from pydantic_evals.evaluators.report_evaluator import ReportEvaluatorContext
from pydantic_evals.otel import SpanTreeRecordingError
from pydantic_evals.reporting.analyses import ReportAnalysis, ScalarResult, TableResult

from invoiceiq.models import ExtractionFailure, RawInvoice
from invoiceiq.scoring import HEADER_FIELDS, score

Extracted = RawInvoice | ExtractionFailure
MONEY_TOLERANCE = Decimal("1")  # spec §17.1: numeric fields match within ±₹1


# --- shared inputs / outputs ----------------------------------------------------------------------


class DocInput(BaseModel):
    """One document from data/invoices. Small and serializable, so it shows in reports."""

    file_id: str
    file_name: str


class CaseMeta(BaseModel):
    category: str


class AuditResult(BaseModel):
    """What the audit task returns: the decision path, not just the auditor's raw report. A case's
    expected_output is one too, with only `flagged` and `decision` filled in."""

    flagged: list[str]  # kinds of warning/critical findings after merging rules + auditor
    decision: str
    llm_findings: list[str] = []
    narrative: str = ""
    escalation: str | None = None
    trajectory: list[str] = []


# --- span capture (spec §17.2: agentic evaluators silently score 0 without it) ---------------------


@dataclass
class SpansCaptured(Evaluator[object, object, object]):
    """The task's spans reached the evaluator. Without Logfire configured they don't, and every
    span-based evaluator (ToolCorrectness, TrajectoryMatch, MaxToolCalls...) quietly fails."""

    def evaluate(self, ctx: EvaluatorContext[object, object, object]) -> EvaluationReason:
        try:
            tree = ctx.span_tree
        except SpanTreeRecordingError as e:
            return EvaluationReason(False, f"no spans recorded: {e}")
        requests = len(tree.find(lambda node: "gen_ai.request.model" in node.attributes))
        return EvaluationReason(requests > 0, f"{requests} model request span(s)")


# --- extraction (§17.1) ---------------------------------------------------------------------------


@dataclass
class KindCorrect(Evaluator[DocInput, Extracted, CaseMeta]):
    """Invoice vs not-an-invoice: the union output routed the right way."""

    def evaluate(self, ctx: EvaluatorContext[DocInput, Extracted, CaseMeta]) -> bool:
        return type(ctx.output) is type(ctx.expected_output)


@dataclass
class FieldAccuracy(Evaluator[DocInput, Extracted, CaseMeta]):
    """Share of fields right (numbers within ±₹1), plus one 0/1 score per header field, so the
    report's averages show which field is weakest."""

    def evaluate(self, ctx: EvaluatorContext[DocInput, Extracted, CaseMeta]) -> dict[str, float]:
        assert ctx.expected_output is not None
        s = score(ctx.expected_output, ctx.output, tolerance=MONEY_TOLERANCE)
        scores = {"field_accuracy": s.accuracy}
        if isinstance(ctx.expected_output, RawInvoice) and isinstance(ctx.output, RawInvoice):
            wrong = {w.split(":", 1)[0] for w in s.wrong}
            scores |= {name: float(name not in wrong) for name in HEADER_FIELDS}
        return scores


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


@dataclass
class LineItemF1(Evaluator[DocInput, Extracted, CaseMeta]):
    """Line items matched by description similarity and quantity, order-free; F1 of the matching."""

    min_similarity: float = 0.6

    def evaluate(self, ctx: EvaluatorContext[DocInput, Extracted, CaseMeta]) -> float:
        want, got = ctx.expected_output, ctx.output
        if not isinstance(want, RawInvoice) or not isinstance(got, RawInvoice):
            return float(type(want) is type(got))
        unmatched = list(got.line_items)
        matched = 0
        for line in want.line_items:
            for candidate in unmatched:
                similar = SequenceMatcher(None, _norm(line.description), _norm(candidate.description)).ratio()
                if similar >= self.min_similarity and candidate.qty == line.qty:
                    unmatched.remove(candidate)
                    matched += 1
                    break
        if not want.line_items and not got.line_items:
            return 1.0
        precision = matched / len(got.line_items) if got.line_items else 0.0
        recall = matched / len(want.line_items) if want.line_items else 0.0
        return 2 * precision * recall / (precision + recall) if matched else 0.0


@dataclass
class ExtractionSummary(ReportEvaluator[DocInput, Extracted, CaseMeta]):
    """Headline metrics: micro-averaged field accuracy, kind accuracy, and how many cases errored."""

    def evaluate(self, ctx: ReportEvaluatorContext[DocInput, Extracted, CaseMeta]) -> list[ReportAnalysis]:
        correct = total = kinds = 0
        for case in ctx.report.cases:
            assert case.expected_output is not None
            s = score(case.expected_output, case.output, tolerance=MONEY_TOLERANCE)
            correct, total, kinds = correct + s.correct, total + s.total, kinds + s.kind_ok
        n = len(ctx.report.cases) + len(ctx.report.failures)  # a failed task counts as wrong
        return [
            ScalarResult(title="field_accuracy", value=correct / total if total else 0.0),
            ScalarResult(title="kind_accuracy", value=kinds / n if n else 0.0),
            ScalarResult(title="task_failures", value=len(ctx.report.failures)),
        ]


# --- audit (§17.2) --------------------------------------------------------------------------------


@dataclass
class FindingRecall(Evaluator[DocInput, AuditResult, CaseMeta]):
    def evaluate(self, ctx: EvaluatorContext[DocInput, AuditResult, CaseMeta]) -> float:
        assert ctx.expected_output is not None
        want = set(ctx.expected_output.flagged)
        return len(want & set(ctx.output.flagged)) / len(want) if want else 1.0


@dataclass
class FindingPrecision(Evaluator[DocInput, AuditResult, CaseMeta]):
    def evaluate(self, ctx: EvaluatorContext[DocInput, AuditResult, CaseMeta]) -> float:
        assert ctx.expected_output is not None
        got = set(ctx.output.flagged)
        return len(got & set(ctx.expected_output.flagged)) / len(got) if got else 1.0


@dataclass
class DecisionMatch(Evaluator[DocInput, AuditResult, CaseMeta]):
    def evaluate(self, ctx: EvaluatorContext[DocInput, AuditResult, CaseMeta]) -> bool:
        assert ctx.expected_output is not None
        return ctx.output.decision == ctx.expected_output.decision


@dataclass
class AuditSummary(ReportEvaluator[DocInput, AuditResult, CaseMeta]):
    """Micro-averaged recall and precision over every finding, overall and per kind."""

    def evaluate(self, ctx: ReportEvaluatorContext[DocInput, AuditResult, CaseMeta]) -> list[ReportAnalysis]:
        per_kind: dict[str, list[int]] = {}  # kind -> [true positives, expected, flagged]
        decisions = 0
        for case in ctx.report.cases:
            assert case.expected_output is not None
            want, got = set(case.expected_output.flagged), set(case.output.flagged)
            for kind in want | got:
                tp_exp_got = per_kind.setdefault(kind, [0, 0, 0])
                tp_exp_got[0] += kind in want and kind in got
                tp_exp_got[1] += kind in want
                tp_exp_got[2] += kind in got
            decisions += case.output.decision == case.expected_output.decision
        tp = sum(v[0] for v in per_kind.values())
        expected, flagged = sum(v[1] for v in per_kind.values()), sum(v[2] for v in per_kind.values())
        n = len(ctx.report.cases) + len(ctx.report.failures)
        rows: list[list[str | int | float | bool | None]] = [
            [
                kind,
                v[1],
                v[2],
                v[0],
                round(v[0] / v[1], 3) if v[1] else None,
                round(v[0] / v[2], 3) if v[2] else None,
            ]
            for kind, v in sorted(per_kind.items())
        ]
        return [
            ScalarResult(title="recall", value=tp / expected if expected else 1.0),
            ScalarResult(title="precision", value=tp / flagged if flagged else 1.0),
            ScalarResult(title="decision_accuracy", value=decisions / n if n else 0.0),
            ScalarResult(title="task_failures", value=len(ctx.report.failures)),
            TableResult(
                title="findings by kind",
                columns=["kind", "expected", "flagged", "caught", "recall", "precision"],
                rows=rows,
            ),
        ]
