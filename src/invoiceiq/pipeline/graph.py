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
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic_ai import RunContext
from pydantic_ai.agent import EventStreamHandler
from pydantic_ai.messages import (
    AgentStreamEvent,
    PartDeltaEvent,
    PartStartEvent,
    ToolCallPart,
    ToolCallPartDelta,
    UserContent,
)
from pydantic_core import from_json
from pydantic_graph import EndMarker, GraphBuilder, StepContext, reduce_list_extend

from invoiceiq.agents.auditor import AuditDeps, AuditorAgent, build_auditor, merge_findings, run_audit
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
from invoiceiq.pipeline.events import EventBus
from invoiceiq.policy import Policy, to_inr
from invoiceiq.reliability import describe_failure
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
    audit_escalation: str | None = None


class StepRunner(Protocol):
    """Runs one unit of our own I/O. Directly by default; as a checkpointed DBOS step in a batch, so
    a recovered workflow returns the recorded result instead of doing the I/O again (M7)."""

    async def __call__[T](self, name: str, fn: Callable[[], Awaitable[T]], /) -> T: ...


async def run_directly[T](name: str, fn: Callable[[], Awaitable[T]], /) -> T:
    return await fn()


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
    # Thinking fixed at construction, for durable runs (D22); None: run_audit adds Thinking per run.
    escalation_auditor: AuditorAgent | None = None
    events: EventBus | None = None  # set by the API so /invoices/{id}/stream can follow the run
    run_step: StepRunner = run_directly  # set by the durable batch to a DBOS step runner


async def pipeline_deps(
    settings: Settings,
    *,
    extractor: ExtractorAgent | None = None,
    auditor: AuditorAgent | None = None,
    poster: PosterAgent | None = None,
    escalation_auditor: AuditorAgent | None = None,
    run_step: StepRunner = run_directly,
) -> PipelineDeps:
    return PipelineDeps(
        run_step=run_step,
        settings=settings,
        policy=Policy.from_settings(settings),
        ledger=open_ledger(settings),
        store=await AppStore.open(settings),
        extractor=extractor or build_extractor(settings),
        auditor=auditor or build_auditor(settings),
        poster=poster or build_poster(settings),
        escalation_auditor=escalation_auditor,
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
            "audit_escalation": s.audit_escalation,
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
    events = ctx.deps.events
    # run() with an event_stream_handler, not run_stream(): run_stream() can't retry a failed
    # output validation, and the extractor relies on those retries (D17).
    handler = partial_json_publisher(events, ctx.state.invoice_id) if events else None
    result = await ctx.deps.extractor.run(
        ctx.inputs,
        deps=deps,
        usage_limits=EXTRACT_LIMITS,
        event_stream_handler=handler,
        timeout=ctx.deps.settings.run_timeout,
        metadata={"invoice_id": ctx.state.invoice_id},  # tags this run's cost-ledger lines
    )
    ctx.state.timings["extract"] = round(time.perf_counter() - start, 3)
    return result.output


@g.step
async def reject(ctx: Ctx[PipelineState, PipelineDeps, ExtractionFailure]) -> InvoiceRecord:
    ctx.state.decision = ctx.deps.policy.decide([], failure=ctx.inputs)
    record = _record(ctx, status="rejected", extraction=ctx.inputs)
    await ctx.deps.run_step("save_rejected", lambda: ctx.deps.store.save_invoice(record))
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

    async def check() -> list[Finding]:
        # A step, not just a read: after this invoice is posted, a replay would see its own posting
        # and call it a duplicate. The recorded result keeps a recovered run on the same path.
        vendor = await ledger.find_vendor(gstin=raw.supplier.gstin, name=raw.supplier.name)
        posted = await ledger.posted_for_vendor(vendor.vendor_id) if vendor else []
        return checks.check_duplicates(raw, vendor, posted)

    return await ctx.deps.run_step("duplicate_check", check)


@g.step
async def po_match(ctx: Ctx[PipelineState, PipelineDeps, RawInvoice]) -> list[Finding]:
    async def check() -> list[Finding]:
        po = await ctx.deps.ledger.get_po(ctx.inputs.po_number) if ctx.inputs.po_number else None
        return checks.check_po(ctx.inputs, po, threshold_pct=ctx.deps.policy.price_drift_pct)

    return await ctx.deps.run_step("po_match", check)


@g.step
async def vendor_status(ctx: Ctx[PipelineState, PipelineDeps, RawInvoice]) -> list[Finding]:
    raw = ctx.inputs

    async def check() -> list[Finding]:
        vendor = await ctx.deps.ledger.find_vendor(gstin=raw.supplier.gstin, name=raw.supplier.name)
        return checks.check_vendor(raw, vendor)

    return await ctx.deps.run_step("vendor_status", check)


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
    outcome = await run_audit(
        ctx.deps.auditor, ctx.state.raw, rule_findings, deps,
        escalate=ctx.deps.settings.audit_escalation, timeout=ctx.deps.settings.run_timeout,
        metadata={"invoice_id": ctx.state.invoice_id}, escalation_agent=ctx.deps.escalation_auditor,
    )  # fmt: skip
    ctx.state.timings["audit"] = round(time.perf_counter() - start, 3)
    ctx.state.findings = merge_findings(rule_findings, outcome.report.findings)
    ctx.state.audit = outcome.report
    ctx.state.audit_escalation = outcome.escalation
    return outcome.report


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
    d = ctx.deps

    async def auto_post() -> InvoiceRecord:
        # One step. If it's interrupted, all of it re-runs: request_posting re-parks (resetting a
        # stale claim) and post_to_ledger's idempotency key returns the first posting, not a second.
        context = await _posting_context(ctx)
        await request_posting(context, poster=d.poster, store=d.store, ledger=d.ledger)
        record = _record(ctx, status="awaiting_approval")
        await d.store.save_invoice(record)
        resolution = await resolve(
            context.invoice_ref,
            approve=True,
            user=POLICY_USER,
            poster=d.poster,
            store=d.store,
            ledger=d.ledger,
        )
        record.status, record.receipt = resolution.status, resolution.receipt
        await d.store.save_invoice(record)
        return record

    return await d.run_step("post", auto_post)


@g.step
async def park(ctx: Ctx[PipelineState, PipelineDeps, Decision]) -> InvoiceRecord:
    # Needs a human: pause the poster on post_to_ledger and store the Conversation for later.
    d = ctx.deps

    async def park_for_approval() -> InvoiceRecord:
        context = await _posting_context(ctx)
        await request_posting(context, poster=d.poster, store=d.store, ledger=d.ledger)
        status = "held" if ctx.inputs.outcome == "held" else "awaiting_approval"
        record = _record(ctx, status=status)
        await d.store.save_invoice(record)
        return record

    return await d.run_step("park", park_for_approval)


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
    """Run the graph. With deps.events set, publish each step as it is scheduled."""
    if deps.events is None:
        return await graph.run(state=PipelineState(), deps=deps, inputs=source)
    events = deps.events
    # graph.iter() hands us each batch of tasks before they run: a step-by-step view of the graph
    # without touching the steps themselves.
    async with graph.iter(state=PipelineState(), deps=deps, inputs=source) as run:
        async for item in run:
            if isinstance(item, EndMarker):
                break
            for task in item:
                events.publish(source.invoice_id, {"type": "step", "node": str(task.node_id)})
    assert run.output is not None
    return run.output


async def process_invoice(source: InvoiceSource, deps: PipelineDeps) -> InvoiceRecord:
    """Run the pipeline; a failure becomes a stored `failed` record with a category, not an exception.

    Used by the API, the CLI and the batch, so a failed invoice always says *why* it failed
    (rate_limited, invalid_output, timeout, usage_limit...) and the rest of a batch carries on.
    """
    try:
        return await run_pipeline(source, deps)
    except Exception as e:  # BaseExceptions (cancellation, KeyboardInterrupt) still propagate
        failed = InvoiceRecord(
            invoice_id=source.invoice_id,
            file_name=source.path.name,
            status="failed",
            error=describe_failure(e),
        )
        await deps.run_step("record_failure", lambda: deps.store.save_invoice(failed))
        if deps.events:
            deps.events.publish(source.invoice_id, {"type": "error", "error": failed.error})
        return failed


def partial_json_publisher(events: EventBus, invoice_id: str) -> EventStreamHandler[ExtractDeps]:
    """Publish the extractor's output tool arguments as they stream: partial JSON, growing.

    These are unvalidated snapshots. stream_output() would validate each one, but a RawInvoice only
    validates once its last required fields (subtotal, total, confidence) arrive, which is the end
    of the stream. A retry starts a new attempt, and the snapshots start over.
    """
    MIN_GROWTH = 120  # characters between snapshots; Claude streams a few characters per delta

    async def handler(ctx: RunContext[ExtractDeps], stream: AsyncIterable[AgentStreamEvent]) -> None:
        attempt = ctx.run_step
        buffer, published = "", 0
        async for event in stream:
            if isinstance(event, PartStartEvent) and isinstance(event.part, ToolCallPart):
                buffer = event.part.args if isinstance(event.part.args, str) else ""
                published = 0
            elif (
                isinstance(event, PartDeltaEvent)
                and isinstance(event.delta, ToolCallPartDelta)
                and isinstance(event.delta.args_delta, str)
            ):
                buffer += event.delta.args_delta
                if len(buffer) - published >= MIN_GROWTH:
                    published = len(buffer)
                    snapshot = from_json(buffer, allow_partial=True)
                    events.publish(invoice_id, {"type": "partial", "attempt": attempt, "invoice": snapshot})
        if buffer and len(buffer) > published:
            events.publish(
                invoice_id,
                {"type": "partial", "attempt": attempt, "invoice": from_json(buffer, allow_partial=True)},
            )

    return handler


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
