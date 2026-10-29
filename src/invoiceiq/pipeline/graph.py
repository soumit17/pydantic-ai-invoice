"""The single-invoice pipeline (spec §9), built with pydantic_graph's GraphBuilder.

The graph is where *we* decide the control flow; the agents decide only inside their own steps.
Three LLM steps (extract, audit, post) sit among deterministic ones, and every branch is a plain
type or value test that a test can drive.

    load_document → extract ─┬─ ExtractionFailure → reject ─────────────────────────────┐
                             └─ RawInvoice → strict_validate → broadcast ┬ duplicate_check   │
                                                                         ├ po_match          │
                                                                         ├ vendor_status     │
                                                                         └ date_and_fx      │
                                   join (reduce_list_extend) → audit → decide ┬ post → end ┤
                                                                              └ park → end ┘

Regenerate docs/pipeline.md with `uv run python -m invoiceiq.pipeline.graph`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai.messages import UserContent
from pydantic_graph import GraphBuilder, StepContext, reduce_list_extend

from invoiceiq.agents.auditor import (
    AUDIT_LIMITS,
    AuditDeps,
    AuditorAgent,
    audit_prompt,
    build_auditor,
    merge_findings,
)
from invoiceiq.agents.extractor import EXTRACT_LIMITS, ExtractDeps, ExtractorAgent, build_extractor
from invoiceiq.agents.poster import PosterAgent, PostingContext, build_poster
from invoiceiq.approvals import POLICY_USER, request_posting, resolve
from invoiceiq.config import PROJECT_ROOT, Settings
from invoiceiq.models import (
    AuditReport,
    Decision,
    ExtractionFailure,
    Finding,
    Invoice,
    InvoiceRecord,
    RawInvoice,
)
from invoiceiq.pipeline import checks
from invoiceiq.pipeline.documents import load_document as read_document
from invoiceiq.policy import Policy, to_inr
from invoiceiq.repos.app_store import AppStore
from invoiceiq.repos.ledger import LedgerRepo, open_ledger

PIPELINE_DOC = PROJECT_ROOT / "docs" / "pipeline.md"
KIND_ORDER = list(checks.SEVERITY)  # parallel checks finish in any order; sort for stable output


@dataclass
class InvoiceSource:
    invoice_id: str
    path: Path


@dataclass
class PipelineState:
    """Mutable, per-run state that steps read and write (inputs/outputs carry the main data flow)."""

    invoice_id: str = ""
    file_name: str = ""
    raw: RawInvoice | None = None
    invoice: Invoice | None = None
    findings: list[Finding] = field(default_factory=lambda: [])
    audit: AuditReport | None = None
    decision: Decision | None = None
    timings: dict[str, float] = field(default_factory=lambda: {})


@dataclass
class PipelineDeps:
    """Everything injected into the steps. Agents are deps so tests can pass FunctionModel-backed ones."""

    settings: Settings
    policy: Policy
    ledger: LedgerRepo
    store: AppStore
    extractor: ExtractorAgent
    auditor: AuditorAgent
    poster: PosterAgent


async def pipeline_deps(
    settings: Settings,
    *,
    extractor: ExtractorAgent | None = None,
    auditor: AuditorAgent | None = None,
    poster: PosterAgent | None = None,
) -> PipelineDeps:
    return PipelineDeps(
        settings=settings,
        policy=Policy.from_settings(settings),
        ledger=open_ledger(settings),
        store=await AppStore.open(settings),
        extractor=extractor or build_extractor(settings),
        auditor=auditor or build_auditor(settings),
        poster=poster or build_poster(settings),
    )


Ctx = StepContext  # short alias for the step signatures below

g = GraphBuilder(
    name="invoice_pipeline",
    state_type=PipelineState,
    deps_type=PipelineDeps,
    input_type=InvoiceSource,
    output_type=InvoiceRecord,
)


def _record(ctx: Ctx[PipelineState, PipelineDeps, object], **fields: object) -> InvoiceRecord:
    s = ctx.state
    return InvoiceRecord.model_validate(
        {
            "invoice_id": s.invoice_id,
            "file_name": s.file_name,
            "extraction": s.raw,
            "findings": s.findings,
            "audit": s.audit,
            "decision": s.decision,
            "timings": s.timings,
            **fields,
        }
    )


# --- Steps ---------------------------------------------------------------------------------------


@g.step
async def load_document(ctx: Ctx[PipelineState, PipelineDeps, InvoiceSource]) -> list[UserContent]:
    ctx.state.invoice_id = ctx.inputs.invoice_id
    ctx.state.file_name = ctx.inputs.path.name
    return read_document(ctx.inputs.path)


@g.step
async def extract(ctx: Ctx[PipelineState, PipelineDeps, list[UserContent]]) -> RawInvoice | ExtractionFailure:
    # LLM step 1. The union return type is what the decision below routes on.
    start = time.perf_counter()
    deps = ExtractDeps(company_gstin=ctx.deps.settings.company_gstin, today=ctx.deps.settings.today())
    result = await ctx.deps.extractor.run(ctx.inputs, deps=deps, usage_limits=EXTRACT_LIMITS)
    ctx.state.timings["extract"] = round(time.perf_counter() - start, 3)
    return result.output


@g.step
async def reject(ctx: Ctx[PipelineState, PipelineDeps, ExtractionFailure]) -> InvoiceRecord:
    ctx.state.decision = ctx.deps.policy.decide([], failure=ctx.inputs)
    record = _record(ctx, status="rejected", extraction=ctx.inputs)
    await ctx.deps.store.save_invoice(record)
    return record


@g.step
async def strict_validate(ctx: Ctx[PipelineState, PipelineDeps, RawInvoice]) -> RawInvoice:
    # Deterministic: arithmetic and GST-type rules become findings, never retries (D4, D10).
    invoice, findings = checks.strict_validate(ctx.inputs)
    ctx.state.raw, ctx.state.invoice, ctx.state.findings = ctx.inputs, invoice, findings
    return ctx.inputs


@g.step
async def duplicate_check(ctx: Ctx[PipelineState, PipelineDeps, RawInvoice]) -> list[Finding]:
    raw, ledger = ctx.inputs, ctx.deps.ledger
    vendor = await ledger.find_vendor(gstin=raw.supplier.gstin, name=raw.supplier.name)
    posted = await ledger.posted_for_vendor(vendor.vendor_id) if vendor else []
    return checks.check_duplicates(raw, vendor, posted)


@g.step
async def po_match(ctx: Ctx[PipelineState, PipelineDeps, RawInvoice]) -> list[Finding]:
    po = await ctx.deps.ledger.get_po(ctx.inputs.po_number) if ctx.inputs.po_number else None
    return checks.check_po(ctx.inputs, po, threshold_pct=ctx.deps.policy.price_drift_pct)


@g.step
async def vendor_status(ctx: Ctx[PipelineState, PipelineDeps, RawInvoice]) -> list[Finding]:
    raw = ctx.inputs
    vendor = await ctx.deps.ledger.find_vendor(gstin=raw.supplier.gstin, name=raw.supplier.name)
    return checks.check_vendor(raw, vendor)


@g.step
async def date_and_fx(ctx: Ctx[PipelineState, PipelineDeps, RawInvoice]) -> list[Finding]:
    return checks.check_date_and_fx(
        ctx.inputs, today=ctx.deps.settings.today(), stale_after_days=ctx.deps.policy.stale_after_days
    )


# A join waits for every branch of the broadcast and folds their outputs with a reducer.
# Each check returns a list, so extend (flatten) rather than append (list of lists).
collect_findings = g.join(reduce_list_extend, initial_factory=list[Finding], node_id="collect_findings")


@g.step
async def audit(ctx: Ctx[PipelineState, PipelineDeps, list[Finding]]) -> AuditReport:
    # LLM step 2: the auditor gets the rule findings as facts and adds only what they miss.
    assert ctx.state.raw is not None
    rule_findings = ctx.state.findings + sorted(ctx.inputs, key=lambda f: KIND_ORDER.index(f.kind))
    deps = AuditDeps(ledger=ctx.deps.ledger, policy=ctx.deps.policy, today=ctx.deps.settings.today())
    start = time.perf_counter()
    result = await ctx.deps.auditor.run(
        audit_prompt(ctx.state.raw, rule_findings, ctx.deps.policy), deps=deps, usage_limits=AUDIT_LIMITS
    )
    ctx.state.timings["audit"] = round(time.perf_counter() - start, 3)
    ctx.state.findings = merge_findings(rule_findings, result.output.findings)
    ctx.state.audit = result.output
    return result.output


@g.step
async def decide(ctx: Ctx[PipelineState, PipelineDeps, AuditReport]) -> Decision:
    # Pure Python: the money decision never comes from a model.
    raw = ctx.state.raw
    assert raw is not None
    decision = ctx.deps.policy.decide(ctx.state.findings, total_inr=to_inr(raw.total, raw.currency))
    ctx.state.decision = decision
    return decision


async def _posting_context(ctx: Ctx[PipelineState, PipelineDeps, Decision]) -> PostingContext:
    raw = ctx.state.raw
    assert raw is not None
    vendor = await ctx.deps.ledger.find_vendor(gstin=raw.supplier.gstin, name=raw.supplier.name)
    # Unknown vendors have no vendor_id, so key them by GSTIN (or name) instead.
    vendor_key = vendor.vendor_id if vendor else (raw.supplier.gstin or raw.supplier.name)
    key = checks.content_hash(vendor_key, raw.invoice_no, raw.invoice_date, raw.total)
    return PostingContext(
        invoice_ref=ctx.state.invoice_id,
        vendor_id=vendor.vendor_id if vendor else None,
        vendor_name=raw.supplier.name,
        invoice_no=raw.invoice_no,
        invoice_date=raw.invoice_date,
        amount=raw.total,
        currency=raw.currency,
        content_hash=key,
        # Derived from the invoice's content, not the upload: the same invoice re-ingested under a
        # new id still maps to the same key, so the ledger can't post it twice.
        idempotency_key=key,
        decision=ctx.inputs.outcome,
    )


@g.step
async def post(ctx: Ctx[PipelineState, PipelineDeps, Decision]) -> InvoiceRecord:
    # LLM step 3, auto path: the poster pauses on post_to_ledger exactly as for a human, and the
    # policy answers through the same resolve() a human approval uses.
    context = await _posting_context(ctx)
    d = ctx.deps
    await request_posting(context, poster=d.poster, store=d.store, ledger=d.ledger)
    record = _record(ctx, status="awaiting_approval")
    await d.store.save_invoice(record)
    resolution = await resolve(
        context.invoice_ref, approve=True, user=POLICY_USER, poster=d.poster, store=d.store, ledger=d.ledger
    )
    record.status, record.receipt = resolution.status, resolution.receipt
    await d.store.save_invoice(record)
    return record


@g.step
async def park(ctx: Ctx[PipelineState, PipelineDeps, Decision]) -> InvoiceRecord:
    # Needs a human: pause the poster on post_to_ledger and store the Conversation for later.
    context = await _posting_context(ctx)
    d = ctx.deps
    await request_posting(context, poster=d.poster, store=d.store, ledger=d.ledger)
    status = "held" if ctx.inputs.outcome == "held" else "awaiting_approval"
    record = _record(ctx, status=status)
    await d.store.save_invoice(record)
    return record


# --- Edges ---------------------------------------------------------------------------------------

g.add(
    g.edge_from(g.start_node).to(load_document),
    g.edge_from(load_document).to(extract),
    # Decision on the *type* extract returned.
    g.edge_from(extract).to(
        g.decision(note="is it an invoice?")
        .branch(g.match(ExtractionFailure).label("not an invoice").to(reject))
        .branch(g.match(RawInvoice).label("invoice").to(strict_validate))
    ),
    # Broadcast: one input, four parallel deterministic checks.
    g.edge_from(strict_validate).to(duplicate_check, po_match, vendor_status, date_and_fx),
    g.edge_from(duplicate_check, po_match, vendor_status, date_and_fx).to(collect_findings),
    g.edge_from(collect_findings).to(audit),
    g.edge_from(audit).to(decide),
    # Decision on a *value*: the policy's outcome.
    g.edge_from(decide).to(
        g.decision(note="policy outcome")
        .branch(
            g.match(Decision, matches=lambda d: d.outcome == "auto_approved").label("auto-approve").to(post)
        )
        .branch(g.match(Decision).label("needs a human").to(park))
    ),
    g.edge_from(reject, post, park).to(g.end_node),
)

graph = g.build()


async def run_pipeline(source: InvoiceSource, deps: PipelineDeps) -> InvoiceRecord:
    return await graph.run(state=PipelineState(), deps=deps, inputs=source)


def render_doc() -> str:
    return (
        "# Pipeline graph\n\n"
        "Generated by `uv run python -m invoiceiq.pipeline.graph` from `graph.render()`. "
        "A test fails if this file is out of date.\n\n"
        f"```mermaid\n{graph.render(title='InvoiceIQ single-invoice pipeline').strip()}\n```\n"
    )


if __name__ == "__main__":
    PIPELINE_DOC.write_text(render_doc(), encoding="utf-8")
    print(f"wrote {PIPELINE_DOC.relative_to(PROJECT_ROOT)}")
