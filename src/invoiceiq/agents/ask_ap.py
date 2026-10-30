"""Ask-AP agent (spec §7.4): a chat assistant for the AP team, with conversation memory.

Memory is a `Conversation`, stored whole in app.sqlite. That means more than the messages: its
running usage makes `ASK_LIMITS` a budget for the *whole conversation*, not a fresh one per turn.

Ledger access is the M3 `ledger_capability`, reused unchanged: `AskDeps` has `.ledger` and
`.today`, so it satisfies the capability's `HasLedger` protocol. M6 swaps it for the MCP server.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.capabilities import MCP, AbstractCapability, Caching, Thinking
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from invoiceiq.capabilities.cost import build_cost_hooks
from invoiceiq.capabilities.ledger import ledger_capability
from invoiceiq.config import Settings
from invoiceiq.mcp_server import Spend, build_mcp_server
from invoiceiq.models import InvoiceStatus, Vendor
from invoiceiq.reliability import build_model
from invoiceiq.repos.app_store import AppStore
from invoiceiq.repos.ledger import LedgerRepo

AskAgent = Agent["AskDeps", str]

# Per conversation, not per turn: the stored Conversation carries the usage forward.
ASK_LIMITS = UsageLimits(request_limit=30)


@dataclass
class AskDeps:
    ledger: LedgerRepo
    store: AppStore
    user: str
    today: date


class InvoiceSummary(BaseModel):
    invoice_id: str
    status: InvoiceStatus
    vendor: str | None
    invoice_no: str | None
    total: str | None
    decision: str | None
    reasons: list[str]
    findings: list[str]
    narrative: str | None
    posting_id: str | None


INSTRUCTIONS = """\
You are Ask AP, an assistant for an Indian accounts-payable team. Answer questions about
invoices, vendors and payments, using the tools. Keep answers short and specific.

- Never invent numbers. For a vendor's spend over a period, use the vendor spend tool; don't add
  up postings yourself. A quarter is three calendar months (Q3 2026 = 1 Jul to 30 Sep 2026); "last
  quarter" is the one before today's.
- To find a vendor from part of its name, use the find vendor tool first, to get its vendor id.
- To explain why an invoice was flagged, held or posted, use explain_invoice.
- If the tools can't answer, say so.
"""


LedgerAccess = Literal["mcp", "local"]


def ledger_access(settings: Settings, mode: LedgerAccess) -> list[AbstractCapability[AskDeps]]:
    """How Ask-AP reaches the ledger.

    mcp:   the MCP capability, the spec's default. Over streamable HTTP when settings.mcp_url is set;
           otherwise the same server connected in-process, which still speaks MCP, without a
           second terminal. tool_prefix=True names the tools ledger_* so they can't collide with
           local tools (the prefix comes from id="ledger").
    local: the M3 ledger_capability object (AskDeps satisfies its HasLedger protocol), as an
           offline fallback.
    """
    if mode == "local":
        return [ledger_capability]
    if settings.mcp_url:
        return [MCP(url=settings.mcp_url, id="ledger", tool_prefix=True)]
    return [MCP(local=build_mcp_server(settings), id="ledger", tool_prefix=True)]


def build_ask_ap(
    settings: Settings, *, model: Model | str | None = None, ledger: LedgerAccess = "mcp"
) -> AskAgent:
    agent: AskAgent = Agent(
        model or build_model(settings, settings.model_dev),
        deps_type=AskDeps,
        output_type=str,
        name="ask_ap",
        description="Answers AP questions about invoices, vendors and spend.",
        instructions=INSTRUCTIONS,
        model_settings=ModelSettings(max_tokens=1024, request_timeout=settings.request_timeout),
        capabilities=[
            Caching(),  # long conversations re-send their history every turn: cache it
            Thinking(effort=False),
            build_cost_hooks(settings),
            *ledger_access(settings, ledger),
        ],
    )

    @agent.instructions(name="session", on_change="append")
    def session(ctx: RunContext[AskDeps]) -> str:
        # on_change="append": when the user or the date changes mid-conversation, the change is
        # appended to the history instead of rewriting the prefix, so the cached prefix stays valid.
        return f"Current user: {ctx.deps.user}. Today is {ctx.deps.today:%d %B %Y}."

    @agent.tool
    async def explain_invoice(ctx: RunContext[AskDeps], invoice_id: str) -> InvoiceSummary:
        """Why an invoice got its status: decision reasons, findings, the auditor's narrative, posting."""
        record = await ctx.deps.store.get_invoice(invoice_id.strip())
        if record is None:
            raise ModelRetry(f"No invoice {invoice_id!r}. Ids look like inv-017; list_invoices shows them.")
        raw = record.extraction if record.extraction and record.extraction.kind == "invoice" else None
        return InvoiceSummary(
            invoice_id=record.invoice_id,
            status=record.status,
            vendor=raw.supplier.name if raw else None,
            invoice_no=raw.invoice_no if raw else None,
            total=f"{raw.currency} {raw.total}" if raw else None,
            decision=record.decision.outcome if record.decision else None,
            reasons=record.decision.reasons if record.decision else [],
            findings=[f"{f.severity} {f.kind}: {f.message}" for f in record.findings],
            narrative=record.audit.narrative if record.audit else None,
            posting_id=record.receipt.posting_id if record.receipt else None,
        )

    @agent.tool
    async def list_invoices(
        ctx: RunContext[AskDeps], status: InvoiceStatus | None = None, limit: int = 20
    ) -> list[tuple[str, str, str]]:
        """Recently processed invoices as (invoice_id, status, file), newest first, optionally by status."""
        records = await ctx.deps.store.list_invoices(status, limit=min(max(limit, 1), 50))
        return [(r.invoice_id, r.status, r.file_name) for r in records]

    if ledger == "local":
        # In mcp mode these come from the server as ledger_find_vendor / ledger_vendor_spend.
        @agent.tool
        async def find_vendors(ctx: RunContext[AskDeps], name_contains: str) -> list[Vendor]:
            """Vendors whose name contains the given text, e.g. 'balaji'."""
            if len(name_contains.strip()) < 2:
                raise ModelRetry("Search with at least 2 characters of the vendor's name.")
            return await ctx.deps.ledger.search_vendors(name_contains)

        @agent.tool
        async def vendor_spend(ctx: RunContext[AskDeps], vendor_id: str, start: date, end: date) -> Spend:
            """Total posted for a vendor between two invoice dates (inclusive), summed per currency."""
            if end < start:
                raise ModelRetry("end must be on or after start.")
            postings = await ctx.deps.ledger.postings_between(vendor_id, start, end)
            totals: dict[str, Decimal] = {}
            for p in postings:
                totals[p.currency] = totals.get(p.currency, Decimal(0)) + p.total
            return Spend(
                vendor_id=vendor_id, start=start, end=end, invoices=len(postings), totals=totals,
                posting_ids=[p.posting_id for p in postings],
            )  # fmt: skip

    return agent


class ChatReply(BaseModel):
    conversation_id: str
    reply: str
    requests_used: int
    requests_limit: int | None


class UnknownConversation(LookupError):
    pass


async def chat(
    message: str, *, conversation_id: str | None, user: str, agent: AskAgent, store: AppStore,
    ledger: LedgerRepo,
    today: date, usage_limits: UsageLimits = ASK_LIMITS,
) -> ChatReply:  # fmt: skip
    """One turn: load the stored Conversation (or start one), run, store the new Conversation."""
    conversation = None
    if conversation_id is not None:
        conversation = await store.get_conversation(conversation_id)
        if conversation is None:
            raise UnknownConversation(conversation_id)
    result = await agent.run(
        message,
        conversation=conversation,  # messages + running usage + id, carried whole
        deps=AskDeps(ledger=ledger, store=store, user=user, today=today),
        usage_limits=usage_limits,
    )
    await store.save_conversation(result.conversation, user=user)
    return ChatReply(
        conversation_id=result.conversation.conversation_id,
        reply=result.output,
        requests_used=result.conversation.usage.requests,
        requests_limit=usage_limits.request_limit,
    )
