"""Poster agent (spec §7.3): posts an invoice to the ledger, but only once a human (or the policy)
approves the tool call.

`post_to_ledger` has `requires_approval=True`, so when the model calls it the run does not execute
it: it ends with `DeferredToolRequests` as the output, and the whole paused state travels in
`result.conversation`. A later run (minutes or days later, in another process) resumes with
`conversation=` and `deferred_tool_results=` holding the approval. See approvals.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pydantic import BaseModel
from pydantic_ai import Agent, DeferredToolRequests, ModelRetry, RunContext, ToolOutput
from pydantic_ai.capabilities import Caching, Thinking
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from invoiceiq.capabilities.cost import build_cost_hooks
from invoiceiq.config import Settings
from invoiceiq.models import DecisionOutcome, PostingReceipt
from invoiceiq.reliability import build_model
from invoiceiq.repos.ledger import LedgerRepo

PosterAgent = Agent["PostDeps", PostingReceipt | DeferredToolRequests | str]

# Request 1: the model calls post_to_ledger (deferred). Request 2, after approval: it returns the receipt.
POST_LIMITS = UsageLimits(request_limit=4, tool_calls_limit=2)
RECEIPT_TOOL = "return_receipt"


class PostingContext(BaseModel):
    """What the poster needs to post one invoice. Stored with the pending approval, so a resumed
    run in a new process has the same facts the first run had."""

    invoice_ref: str
    vendor_id: str | None
    vendor_name: str
    invoice_no: str
    invoice_date: date
    amount: Decimal
    currency: str
    content_hash: str
    idempotency_key: str
    decision: DecisionOutcome


@dataclass
class PostDeps:
    ledger: LedgerRepo
    context: PostingContext
    posted_by: str  # per run: the resumed run gets the approver's identity, not the requester's


INSTRUCTIONS = """\
You post vendor invoices to the ledger.
Call post_to_ledger exactly once, copying the arguments given in the request unchanged.
invoice_ref is our internal document id (like inv-027). It is meant to differ from the vendor's
invoice number, so a mismatch between the two is expected and is not a reason to stop.
When post_to_ledger returns the posting, return it as the receipt.
If an approver denies the call, reply in one sentence: the invoice was not posted, and the
approver's reason, quoted.
"""


def posting_request(context: PostingContext) -> str:
    args = {
        "invoice_ref": context.invoice_ref,
        "amount": str(context.amount),
        "idempotency_key": context.idempotency_key,
    }
    return (
        f"Post our document {context.invoice_ref}: vendor invoice {context.invoice_no} from "
        f"{context.vendor_name}, {context.currency} {context.amount}.\n"
        f"Arguments: {json.dumps(args)}"
    )


def build_poster(settings: Settings, *, model: Model | str | None = None) -> PosterAgent:
    agent: PosterAgent = Agent(
        model or build_model(settings, settings.model_dev),
        deps_type=PostDeps,
        # DeferredToolRequests in the output type is what lets a run *end* on a pending approval
        # instead of executing the tool; str covers the reply after a denial.
        output_type=[
            ToolOutput(PostingReceipt, name=RECEIPT_TOOL, description="Return the ledger posting."),
            DeferredToolRequests,
            str,
        ],
        name="poster",
        description="Posts an approved invoice to the ledger (the call itself needs approval).",
        instructions=INSTRUCTIONS,
        model_settings=ModelSettings(max_tokens=512, request_timeout=settings.request_timeout),
        capabilities=[Caching(), Thinking(effort=False), build_cost_hooks(settings)],
    )

    @agent.tool(requires_approval=True)
    async def post_to_ledger(
        ctx: RunContext[PostDeps], invoice_ref: str, amount: Decimal, idempotency_key: str
    ) -> PostingReceipt:
        """Post the invoice to the ledger. Requires approval. invoice_ref is our internal document
        id, not the vendor's invoice number. Posting the same idempotency key again returns the
        original posting instead of creating a second one."""
        # Only runs after approval. The checks guard against a model that changed the arguments;
        # an approver's edited amount (a partial posting) arrives here as an overridden argument.
        c = ctx.deps.context
        if invoice_ref != c.invoice_ref or idempotency_key != c.idempotency_key:
            raise ModelRetry(
                f"Use invoice_ref {c.invoice_ref!r} and idempotency_key {c.idempotency_key!r} as given."
            )
        if not Decimal(0) < amount <= c.amount:
            raise ModelRetry(f"amount must be more than 0 and at most the invoice total {c.amount}.")
        posted, _created = await ctx.deps.ledger.post_invoice(
            vendor_id=c.vendor_id,
            invoice_no=c.invoice_no,
            invoice_date=c.invoice_date,
            amount=amount,
            currency=c.currency,
            content_hash=c.content_hash,
            posted_by=ctx.deps.posted_by,
            idempotency_key=c.idempotency_key,
        )
        return PostingReceipt(
            posting_id=posted.posting_id,
            idempotency_key=posted.idempotency_key,
            amount=posted.total,
            posted_by=posted.posted_by,
            posted_at=posted.posted_at,
        )

    return agent
