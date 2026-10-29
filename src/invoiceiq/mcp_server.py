"""The ledger as an MCP server (spec §12): one server for our agents *and* for other MCP clients,
such as Claude Code.

Built on the official MCP SDK. In mcp 2.x the class formerly called `FastMCP` is `MCPServer`
(D18). Read-only: nothing here can post or change the ledger.

    uv run invoiceiq mcp                        # stdio (what Claude Code launches)
    uv run invoiceiq mcp --http 127.0.0.1:8765  # streamable HTTP at http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from invoiceiq import gst
from invoiceiq.config import Settings
from invoiceiq.models import PurchaseOrder, Vendor
from invoiceiq.policy import Policy
from invoiceiq.repos.app_store import AppStore
from invoiceiq.repos.ledger import open_ledger

INSTRUCTIONS = (
    "Read-only access to the InvoiceIQ accounts-payable ledger: vendors, purchase orders, posted "
    "invoices and invoices waiting for approval. Amounts are in the invoice currency (usually INR)."
)


class Spend(BaseModel):
    vendor_id: str
    start: date
    end: date
    invoices: int
    totals: dict[str, Decimal]  # per currency
    posting_ids: list[str]


class FlaggedInvoice(BaseModel):
    invoice_id: str
    status: str
    vendor: str | None
    invoice_no: str | None
    total: str | None
    reasons: list[str]


def build_mcp_server(settings: Settings) -> MCPServer:
    server = MCPServer("invoiceiq-ledger", instructions=INSTRUCTIONS, version="0.1.0")
    ledger = open_ledger(settings)
    policy = Policy.from_settings(settings)

    @server.tool()
    async def find_vendor(query: str) -> list[Vendor]:
        """Find vendors by GSTIN (exact) or by part of their name (case-insensitive)."""
        query = query.strip()
        if gst.is_valid_gstin(query.upper()):
            vendor = await ledger.find_vendor(gstin=query.upper(), name="")
            return [vendor] if vendor else []
        return await ledger.search_vendors(query)

    @server.tool()
    async def get_purchase_order(po_number: str) -> PurchaseOrder | None:
        """A purchase order and its lines (agreed unit prices). PO numbers look like PO-2026-0007."""
        return await ledger.get_po(po_number.strip().upper())

    @server.tool()
    async def vendor_spend(vendor_id: str, start: date, end: date) -> Spend:
        """Total posted for a vendor between two invoice dates (inclusive), summed per currency.
        Vendor ids look like V007 (use find_vendor first)."""
        postings = await ledger.postings_between(vendor_id, start, end)
        totals: dict[str, Decimal] = {}
        for p in postings:
            totals[p.currency] = totals.get(p.currency, Decimal(0)) + p.total
        return Spend(
            vendor_id=vendor_id, start=start, end=end, invoices=len(postings), totals=totals,
            posting_ids=[p.posting_id for p in postings],
        )  # fmt: skip

    @server.tool()
    async def list_flagged_invoices(status: str = "awaiting_approval") -> list[FlaggedInvoice]:
        """Invoices in a given status: awaiting_approval, held, rejected, denied, failed or posted."""
        store = await AppStore.open(settings)
        flagged: list[FlaggedInvoice] = []
        for r in await store.list_invoices(status):
            raw = r.extraction if r.extraction and r.extraction.kind == "invoice" else None
            flagged.append(
                FlaggedInvoice(
                    invoice_id=r.invoice_id,
                    status=r.status,
                    vendor=raw.supplier.name if raw else None,
                    invoice_no=raw.invoice_no if raw else None,
                    total=f"{raw.currency} {raw.total}" if raw else None,
                    reasons=r.decision.reasons if r.decision else [],
                )
            )
        return flagged

    @server.resource("ledger://policy", mime_type="text/markdown")
    def approval_policy() -> str:
        """The approval policy that decides auto-approve, approval, hold and reject."""
        return (
            "# InvoiceIQ approval policy\n\n"
            "| Condition | Decision |\n|---|---|\n"
            f"| No findings and total ≤ ₹{policy.auto_approve_limit_inr} | Auto-approve and post |\n"
            f"| No findings and total > ₹{policy.auto_approve_limit_inr} | Needs approval |\n"
            "| Any warning finding | Needs approval |\n"
            "| Any critical finding from a deterministic rule | Hold (approval needs a note) |\n"
            "| Extraction failure | Rejected |\n\n"
            f"Price drift is flagged above {policy.price_drift_pct}% over the PO price. Invoices older "
            f"than {policy.stale_after_days} days are stale. A finding from the LLM auditor can send an "
            "invoice to a human but never hold it.\n"
        )

    return server
