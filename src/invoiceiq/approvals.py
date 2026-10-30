"""Human approval for ledger postings (spec §10), built on deferred tools.

    request_posting  run the poster until it pauses on post_to_ledger; store the Conversation
    resolve          load it (any process, any time later), answer the pending call with
                     approve / approve-with-edited-amount / deny, and resume the run

The policy's auto-approval goes through `resolve` too, with user="policy": one code path for
every posting, so what is tested for humans is what runs for the policy.

Double-approval protection has two layers: the pending row is claimed atomically
(pending → resolving), and the ledger insert is idempotent on the key.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pydantic_ai import DeferredToolRequests, ToolApproved, ToolDenied
from pydantic_ai.tools import DeferredToolApprovalResult

from invoiceiq.agents.poster import POST_LIMITS, PostDeps, PosterAgent, PostingContext, posting_request
from invoiceiq.capabilities.guards import REVIEWER_USER
from invoiceiq.models import InvoiceStatus, PostingReceipt
from invoiceiq.repos.app_store import AppStore
from invoiceiq.repos.ledger import LedgerRepo

POLICY_USER = "policy"


class ApprovalError(ValueError):
    """The approval can't be applied as asked (unknown invoice, missing note, bad amount...)."""


@dataclass
class Resolution:
    invoice_id: str
    status: InvoiceStatus  # posted, denied, or still awaiting_approval/held if the model asked again
    receipt: PostingReceipt | None
    already_resolved: bool = False
    message: str | None = None


async def request_posting(
    context: PostingContext, *, poster: PosterAgent, store: AppStore, ledger: LedgerRepo
) -> PostingReceipt | None:
    """Run the poster until it asks to call post_to_ledger, then park the paused Conversation.

    Returns None once parked, or the receipt if the ApprovalReviewer approved the call on a
    person's behalf (M8): the call then runs in this same run and nothing is left to park.
    """
    result = await poster.run(
        posting_request(context),
        # Only the reviewer can approve within this first run, so a posting made here is its.
        deps=PostDeps(ledger=ledger, context=context, posted_by=REVIEWER_USER),
        usage_limits=POST_LIMITS,
        metadata={"invoice_id": context.invoice_ref},
    )
    if not isinstance(result.output, DeferredToolRequests):
        if receipt := await _receipt(ledger, context.idempotency_key):
            return receipt
        raise ApprovalError(f"the poster did not ask to post {context.invoice_ref}: {result.output!r}")
    await store.save_pending(context, result.conversation)
    return None


async def resolve(
    invoice_id: str,
    *,
    approve: bool,
    user: str,
    note: str | None = None,
    amount: Decimal | None = None,
    poster: PosterAgent,
    store: AppStore,
    ledger: LedgerRepo,
) -> Resolution:
    pending = await store.get_pending(invoice_id)
    if pending is None:
        raise ApprovalError(f"no pending approval for {invoice_id}")
    context = pending.context

    if pending.status in ("approved", "denied"):
        # Approving twice is a no-op: return what happened the first time, with no model call.
        receipt = await _receipt(ledger, context.idempotency_key)
        status: InvoiceStatus = "posted" if receipt else "denied"
        return Resolution(invoice_id, status, receipt, already_resolved=True)
    if approve and context.decision == "held" and not (note and note.strip()):
        raise ApprovalError("held invoices can only be approved with a note")
    if amount is not None and not Decimal(0) < amount <= context.amount:
        raise ApprovalError(f"amount must be more than 0 and at most {context.amount}")

    requests = pending.conversation.deferred_tool_requests
    if requests is None or not requests.approvals:
        raise ApprovalError(f"the stored conversation for {invoice_id} has no pending tool call")
    if not await store.claim_pending(invoice_id):
        raise ApprovalError(f"{invoice_id} is already being resolved")

    try:
        answers: dict[str, bool | DeferredToolApprovalResult] = {}
        for call in requests.approvals:
            if not approve:
                # Say who denied it and why: the model relays this, and it must not read like a ledger error.
                reason = f": {note}" if note else " (no reason given)"
                answers[call.tool_call_id] = ToolDenied(f"An approver ({user}) denied this posting{reason}")
            elif amount is not None:
                # Approve with edited arguments: the tool runs with these instead of the model's.
                answers[call.tool_call_id] = ToolApproved(
                    override_args={**call.args_as_dict(), "amount": str(amount)}
                )
            else:
                answers[call.tool_call_id] = True
        result = await poster.run(
            conversation=pending.conversation,  # messages + usage + the pending requests, whole
            deferred_tool_results=requests.build_results(approvals=answers),
            deps=PostDeps(ledger=ledger, context=context, posted_by=user),
            usage_limits=POST_LIMITS,
            metadata={"invoice_id": context.invoice_ref},
        )
    except BaseException:
        await store.release_pending(invoice_id)
        raise

    if isinstance(result.output, DeferredToolRequests):
        # The model asked to post again (e.g. after a ModelRetry). Park the new conversation.
        await store.save_pending(context, result.conversation)
        waiting: InvoiceStatus = "held" if context.decision == "held" else "awaiting_approval"
        return Resolution(invoice_id, waiting, None)

    # The ledger, not the model's reply, is the source of truth for what was posted.
    receipt = await _receipt(ledger, context.idempotency_key) if approve else None
    await store.finish_pending(invoice_id, status="approved" if approve else "denied", by=user, note=note)
    record = await store.get_invoice(invoice_id)
    if record is not None:
        record.status = "posted" if receipt else "denied"
        record.receipt = receipt
        await store.save_invoice(record)
    message = result.output if isinstance(result.output, str) else None
    return Resolution(invoice_id, "posted" if receipt else "denied", receipt, message=message)


async def _receipt(ledger: LedgerRepo, idempotency_key: str) -> PostingReceipt | None:
    posted = await ledger.get_posting(idempotency_key)
    if posted is None:
        return None
    return PostingReceipt(
        posting_id=posted.posting_id,
        idempotency_key=posted.idempotency_key,
        amount=posted.total,
        posted_by=posted.posted_by,
        posted_at=posted.posted_at,
    )
