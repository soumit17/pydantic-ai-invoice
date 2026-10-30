"""Batches (spec §9.2, §11): many invoices through the single-invoice pipeline.

Two ways, for two jobs:

1. `run_batch_in_process`: a pydantic_graph that `.map()`s the pipeline over a list of invoices
   and joins the results. Fast and simple, but if the process dies, the batch is gone.

2. `process_batch_workflow`: a DBOS workflow that survives `kill -9`. Each invoice is its own child
   workflow, on a DBOS queue that caps how many run at once. Inside it, the extractor's and auditor's
   model requests are DBOS steps (`DBOSDurability`), and our own I/O (ledger checks, posting,
   parking, store writes) goes through `deps.run_step` as DBOS steps. On restart, `DBOS.launch()`
   recovers the batch: finished invoices return their recorded results, and a half-finished one
   resumes, replaying its completed steps from the checkpoint instead of calling the model again.

Durable execution keeps one *run* alive through crashes. That's a different job from storing a
`Conversation` (Ask-AP, approvals), which keeps a *thread* across requests (spec §11.4).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbos import DBOS, DBOSConfig, Queue, SetWorkflowID, WorkflowHandleAsync
from pydantic_graph import GraphBuilder, StepContext, reduce_list_append

from invoiceiq.agents.auditor import AuditorAgent, build_auditor
from invoiceiq.agents.extractor import ExtractorAgent, build_extractor
from invoiceiq.agents.poster import PosterAgent
from invoiceiq.config import Settings
from invoiceiq.models import InvoiceRecord
from invoiceiq.pipeline.graph import InvoiceSource, PipelineDeps, pipeline_deps, process_invoice

# --- 1. In-process batch: a graph that maps the pipeline over a list -------------------------------

bg = GraphBuilder(
    name="invoice_batch",
    deps_type=PipelineDeps,
    input_type=list[InvoiceSource],
    output_type=list[InvoiceRecord],
)


@bg.step
async def process_one(ctx: StepContext[None, PipelineDeps, InvoiceSource]) -> InvoiceRecord:
    return await process_invoice(ctx.inputs, ctx.deps)


collect_records = bg.join(reduce_list_append, initial_factory=list[InvoiceRecord], node_id="collect_records")

bg.add(
    # .map(): one parallel branch per invoice in the input list. downstream_join_id tells the graph
    # where to go when the list is empty. Model concurrency is capped by the shared limiter (M7).
    bg.edge_from(bg.start_node).map(downstream_join_id=collect_records.id).to(process_one),
    bg.edge_from(process_one).to(collect_records),
    bg.edge_from(collect_records).to(bg.end_node),
)
batch_graph = bg.build()


async def run_batch_in_process(sources: Sequence[InvoiceSource], deps: PipelineDeps) -> list[InvoiceRecord]:
    records = await batch_graph.run(deps=deps, inputs=list(sources))
    return sorted(records, key=lambda r: r.invoice_id)  # the join collects in completion order


# --- 2. Durable batch: DBOS workflows ------------------------------------------------------------

QUEUE_NAME = "invoiceiq_invoices"
BATCH_CONCURRENCY = 4  # invoices processed at once (each one's model calls also pass the limiter)


async def invoice_queue() -> Queue:
    """The DBOS queue that caps concurrent invoices. In dbos 3.x a queue is registered in the system
    database, which needs a launched DBOS, so it's looked up (and registered if missing) on use:
    idempotent, and safe even if a recovered batch runs before start_runtime() registers it."""
    queue = await DBOS.retrieve_queue_async(QUEUE_NAME)
    if queue is None:
        queue = await DBOS.register_queue_async(
            QUEUE_NAME, worker_concurrency=BATCH_CONCURRENCY, polling_interval_sec=0.2
        )
    return queue


@dataclass
class BatchRuntime:
    settings: Settings
    deps: PipelineDeps


_runtime: BatchRuntime | None = None


def _require_runtime() -> BatchRuntime:
    if _runtime is None:
        raise RuntimeError("the batch runtime isn't started; call start_runtime() first")
    return _runtime


async def dbos_step[T](name: str, fn: Callable[[], Awaitable[T]], /) -> T:
    """The batch's step runner: our own I/O as a checkpointed DBOS step."""

    async def call() -> T:  # a coroutine function, which is what run_step_async's async overload takes
        return await fn()

    return await DBOS.run_step_async({"name": name}, call)


def _summary(record: InvoiceRecord) -> dict[str, Any]:
    # Workflow results are checkpointed, so keep them small and plain.
    return {
        "invoice_id": record.invoice_id,
        "status": record.status,
        "decision": record.decision.outcome if record.decision else None,
        "posting_id": record.receipt.posting_id if record.receipt else None,
        "error": record.error,
    }


@DBOS.workflow(name="invoiceiq_process_invoice")
async def process_invoice_workflow(invoice_id: str, path: str) -> dict[str, Any]:
    record = await process_invoice(InvoiceSource(invoice_id, Path(path)), _require_runtime().deps)
    return _summary(record)


@DBOS.workflow(name="invoiceiq_process_batch")
async def process_batch_workflow(batch_id: str, items: list[tuple[str, str]]) -> list[dict[str, Any]]:
    # Enqueue in a fixed order with fixed child ids, so a recovered batch finds the same children
    # (and their recorded results) instead of starting new ones.
    queue = await invoice_queue()
    handles: list[WorkflowHandleAsync[dict[str, Any]]] = []
    for invoice_id, path in items:
        with SetWorkflowID(f"{batch_id}:{invoice_id}"):
            handles.append(await queue.enqueue_async(process_invoice_workflow, invoice_id, path))
    return [await h.get_result() for h in handles]


async def start_runtime(
    settings: Settings,
    *,
    extractor: ExtractorAgent | None = None,
    auditor: AuditorAgent | None = None,
    escalation_auditor: AuditorAgent | None = None,
    poster: PosterAgent | None = None,
) -> BatchRuntime:
    """Build durable agents, configure DBOS on SQLite, and launch it (which recovers crashed batches).

    The agents are built before launch: DBOSDurability registers their steps, and a recovered
    workflow may need them the moment `launch()` returns. Their names ("extractor", "auditor") are
    part of the recorded history, so they must never be renamed.
    """
    global _runtime
    deps = await pipeline_deps(
        settings,
        extractor=extractor or build_extractor(settings, durable=True),
        auditor=auditor or build_auditor(settings, durable=True),
        # Escalation can't add Thinking per run inside a workflow, so it's a second agent (D22).
        escalation_auditor=escalation_auditor or build_auditor(settings, durable=True, thinking="medium"),
        poster=poster,
        run_step=dbos_step,
    )
    DBOS(
        config=DBOSConfig(
            name="invoiceiq",
            system_database_url=settings.dbos_url,  # SQLite: no Postgres needed
            # A fixed version: DBOS only recovers workflows recorded under the same version.
            application_version=settings.application_version,
        )
    )
    _runtime = BatchRuntime(settings=settings, deps=deps)
    DBOS.launch()
    await invoice_queue()  # register the queue right away, so recovered children get dequeued
    return _runtime


def stop_runtime() -> None:
    global _runtime
    DBOS.destroy()
    _runtime = None


async def submit_batch(
    batch_id: str, items: Sequence[tuple[str, Path]]
) -> WorkflowHandleAsync[list[dict[str, Any]]]:
    """Start a batch, or attach to it if this id already exists (starting is idempotent per id)."""
    with SetWorkflowID(batch_id):
        return await DBOS.start_workflow_async(
            process_batch_workflow, batch_id, [(i, str(p)) for i, p in items]
        )


async def batch_status(batch_id: str) -> dict[str, Any] | None:
    parent = await DBOS.get_workflow_status_async(batch_id)
    if parent is None:
        return None
    children = await DBOS.list_workflows_async(
        workflow_id_prefix=f"{batch_id}:", load_input=False, load_output=False
    )
    counts: dict[str, int] = {}
    for child in children:
        counts[child.status] = counts.get(child.status, 0) + 1
    return {"batch_id": batch_id, "status": parent.status, "invoices": counts}
