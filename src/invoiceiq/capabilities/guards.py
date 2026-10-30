"""Harness guards on the poster (spec §10.3, M8): a second, cheap model on each side of an approval.

    ApprovalReviewer  can only say *yes* on a person's behalf: it answers pending approvals in the
                      high-value-but-clean tier, so a person no longer rubber-stamps them.
    ToolCallJudge     can only say *no*: it runs when post_to_ledger is about to execute (after any
                      approval) and blocks a call that would post more than the invoice total.

Both run an internal Harness agent that our capabilities never see (D23), so their model is wrapped
in `CostRecordingModel` to keep the cost ledger complete.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext, ToolDefinition
from pydantic_ai_harness import ApprovalReviewer, ToolCallJudge
from pydantic_ai_harness.approval_reviewer import ApprovalReview
from pydantic_ai_harness.tool_call_judge import ToolCallVerdict

from invoiceiq.config import Settings
from invoiceiq.shared.cost import record_response

if TYPE_CHECKING:
    from invoiceiq.agents.poster import PostDeps

POST_TOOL = "post_to_ledger"
REVIEWER_USER = "approval-reviewer"  # posted_by on the ledger when the reviewer approved the call

# Each answer is one word (yes / no / unsure) through an output tool; thinking off (Haiku would
# otherwise think by default and share these tokens with the answer, D11).
GUARD_SETTINGS = ModelSettings(max_tokens=256, thinking=False)

REVIEW_QUESTION = (
    "Should a person look at this ledger posting before it goes through? Answer yes if anything "
    "about the vendor, the amount or the arguments looks unusual or inconsistent with the request; "
    "answer no if it is a routine posting of the invoice exactly as requested."
)
JUDGE_QUESTION = (
    "Is the `amount` argument of this call more than the invoice total stated in the posting "
    "request, by more than 1 (in the invoice's currency)? A smaller amount is a partial posting "
    "and is fine."
)


class CostRecordingModel(WrapperModel):
    """Records each response of an internal Harness agent in the project cost ledger.

    A WrapperModel, not a hook: the judge's and reviewer's agents are built inside the Harness
    capabilities, so the only part of them we choose is the model.
    """

    def __init__(self, wrapped: Model, *, settings: Settings, label: str) -> None:
        super().__init__(wrapped)
        self._costs_path = settings.costs_path
        self._label = label

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        response = await super().request(messages, model_settings, model_request_parameters)
        record_response(response, path=self._costs_path, label=self._label)
        return response


def guard_model(settings: Settings, label: str) -> Model:
    """The cheap model (Haiku) with explicit max_tokens, wrapped for the cost ledger."""
    name = settings.model_dev.removeprefix("anthropic:")
    return CostRecordingModel(AnthropicModel(name, settings=GUARD_SETTINGS), settings=settings, label=label)


def in_review_tier(ctx: RunContext[PostDeps], tool_def: ToolDefinition) -> bool:
    """The only calls the reviewer answers: post_to_ledger for an invoice that needs approval only
    because of its amount (> the auto-approve limit, no findings). Anything with a finding, and
    anything held, still goes to a person."""
    c = ctx.deps.context
    return tool_def.name == POST_TOOL and c.decision == "awaiting_approval" and not c.findings


def build_reviewer(
    model: Model, *, on_review: Callable[[ApprovalReview], None] | None = None
) -> ApprovalReviewer[PostDeps]:
    return ApprovalReviewer(
        model,
        question=REVIEW_QUESTION,
        tools=in_review_tier,
        # The call's arguments alone don't name the vendor or the request; the conversation does.
        include_conversation=True,
        on_review=on_review,
        id="high-value-reviewer",  # names its durable operation, should the poster ever run durably
    )


def build_amount_judge(
    model: Model, *, on_verdict: Callable[[ToolCallVerdict], None] | None = None
) -> ToolCallJudge[PostDeps]:
    return ToolCallJudge(
        model,
        question=JUDGE_QUESTION,
        tools=[POST_TOOL],
        # The invoice total is in the posting request, not in the call's arguments.
        include_conversation=True,
        # Fail open: post_to_ledger itself refuses amount > total in code, which is the hard
        # boundary. Failing closed would turn a judge outage into denied postings (D23).
        on_uncertain="allow",
        # Returned to the poster in place of the tool's result; it relays it as its reply.
        denial_message=(
            "The amount judge blocked {tool_name}: the amount is more than the invoice total. "
            "Do not retry; reply in one sentence that the posting was blocked."
        ),
        on_verdict=on_verdict,
        id="amount-judge",
    )
