"""LedgerCapability (spec §8.4): read-only ledger tools, bundled with the instructions for using them.

A declarative `Capability` rather than a bare toolset, because the instructions travel *with* the
tools: any agent that gets the tools also gets "check vendor and PO before judging drift".

It is typed against the `HasLedger` protocol, not a concrete deps class. Deps are contravariant,
so the same capability attaches to the auditor (`AuditDeps`) and, in M5, to Ask-AP.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Protocol

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import Capability

from invoiceiq import gst
from invoiceiq.models import PostedInvoice, PurchaseOrder, Vendor
from invoiceiq.repos.ledger import LedgerRepo


class HasLedger(Protocol):
    @property
    def ledger(self) -> LedgerRepo: ...
    @property
    def today(self) -> date: ...


PO_PATTERN = re.compile(r"^PO-\d{4}-\d{4}$")
VENDOR_ID_PATTERN = re.compile(r"^V\d{3}$")

ledger_capability = Capability[HasLedger](
    id="ledger",
    description="Read-only access to vendors, purchase orders and posting history.",
    instructions=(
        "Ledger tools: always check vendor status and the PO lines before judging price drift, and "
        "search posted invoices before judging a possible duplicate. Treat a tool result as fact."
    ),
)


@ledger_capability.tool
async def get_vendor(
    ctx: RunContext[HasLedger], gstin: str | None = None, name: str | None = None
) -> Vendor | None:
    """Look up a vendor by GSTIN. Foreign vendors have no GSTIN: pass their exact name instead.

    Returns None when the vendor is not in the vendor master.
    """
    if gstin:
        gstin = gstin.strip().upper()
        if not gst.is_valid_gstin(gstin):
            # ModelRetry from a tool: the message goes back to the model, which can fix its call.
            raise ModelRetry(f"{gstin!r} is not a valid GSTIN. It has 15 characters, like 19ABCDE1234F1Z5.")
        return await ctx.deps.ledger.find_vendor(gstin=gstin, name="")
    if name:
        return await ctx.deps.ledger.find_vendor(gstin=None, name=name)
    raise ModelRetry("Pass either gstin or, for a foreign vendor without one, name.")


@ledger_capability.tool
async def get_po(ctx: RunContext[HasLedger], po_number: str) -> PurchaseOrder | None:
    """Fetch a purchase order and its lines (agreed unit prices). Returns None if it doesn't exist."""
    po_number = po_number.strip().upper()
    if not PO_PATTERN.fullmatch(po_number):
        raise ModelRetry(f"{po_number!r} is not a PO number. They look like PO-YYYY-NNNN, e.g. PO-2026-0007.")
    return await ctx.deps.ledger.get_po(po_number)


@ledger_capability.tool
async def vendor_history(ctx: RunContext[HasLedger], vendor_id: str, months: int = 6) -> list[PostedInvoice]:
    """Invoices already posted for a vendor in the last `months` months (1-24), oldest first."""
    if not VENDOR_ID_PATTERN.fullmatch(vendor_id):
        raise ModelRetry(f"{vendor_id!r} is not a vendor id. Ids look like V007; get one from get_vendor.")
    if not 1 <= months <= 24:
        raise ModelRetry("months must be between 1 and 24.")
    return await ctx.deps.ledger.vendor_history(vendor_id, months=months, as_of=ctx.deps.today)


@ledger_capability.tool
async def search_posted(ctx: RunContext[HasLedger], query: str) -> list[PostedInvoice]:
    """Find posted invoices whose number contains `query`. Punctuation and case are ignored, so
    'INV/26/042' also finds 'INV-26-042'. Returns at most 10, newest first."""
    if len(re.sub(r"[^A-Za-z0-9]", "", query)) < 3:
        raise ModelRetry("Search with at least 3 letters or digits of the invoice number.")
    return await ctx.deps.ledger.search_posted(query)
