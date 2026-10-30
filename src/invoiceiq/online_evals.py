"""Online evaluation of the auditor in production (spec §17.4, M8).

`OnlineEvaluation` runs evaluators in the background after a sampled share of agent runs and emits
each result as a `gen_ai.evaluation.result` OTel event on the run's trace, so quality is watched on
real traffic, not only in offline evals. Only cheap, deterministic evaluators here: an LLM judge on
every sampled run would cost real money.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_evals.evaluators import Evaluator, EvaluatorContext, MaxToolCalls
from pydantic_evals.online import OnlineEvaluator
from pydantic_evals.online_capability import OnlineEvaluation

from invoiceiq.models import AuditReport

# The auditor's usage limit allows more; a run near it is worth looking at (spec §17.2 uses 8 too).
MAX_AUDIT_TOOL_CALLS = 8


@dataclass
class NarrativeNotEmpty(Evaluator[object, AuditReport, object]):
    """The approver reads the narrative; an empty or one-word one is a silent quality failure."""

    min_words: int = 5

    def evaluate(self, ctx: EvaluatorContext[object, AuditReport, object]) -> bool:
        return len(ctx.output.narrative.split()) >= self.min_words


def auditor_online_evaluation(sample_rate: float) -> OnlineEvaluation[object]:
    return OnlineEvaluation(
        evaluators=[
            # Reads the run's tool-call spans: needs Logfire/OTel configured, like the offline agentic evals.
            OnlineEvaluator(evaluator=MaxToolCalls(max_calls=MAX_AUDIT_TOOL_CALLS), sample_rate=sample_rate),
            OnlineEvaluator(evaluator=NarrativeNotEmpty(), sample_rate=sample_rate),
        ]
    )
