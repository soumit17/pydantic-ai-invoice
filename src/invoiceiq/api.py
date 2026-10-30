"""HTTP API (spec §13).

Two dependency-injection systems meet here. FastAPI's `Depends` hands each route the shared
`Services` built once in the lifespan; the route then builds the *agent* deps
(ExtractDeps, AuditDeps, PostDeps, AskDeps) from them for each run. FastAPI owns the
app-lifetime objects; Pydantic AI's deps are per-run and carry per-request facts (who is asking).

    uv run invoiceiq serve
    curl -s -X POST localhost:8000/invoices -H 'content-type: application/json' \
         -d '{"path": "data/invoices/inv-017.pdf"}'
    curl -N localhost:8000/invoices/inv-017/stream
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ValidationError
from pydantic_ai.exceptions import UsageLimitExceeded
from sse_starlette import EventSourceResponse

# request.form() yields Starlette's UploadFile; FastAPI's is a subclass, so isinstance needs this one.
from starlette.datastructures import UploadFile

from invoiceiq.agents.ask_ap import AskAgent, ChatReply, UnknownConversation, build_ask_ap, chat
from invoiceiq.approvals import ApprovalError, resolve
from invoiceiq.config import PROJECT_ROOT, Settings, get_settings
from invoiceiq.models import InvoiceRecord
from invoiceiq.pipeline.batch import batch_status, start_runtime, stop_runtime, submit_batch
from invoiceiq.pipeline.events import EventBus
from invoiceiq.pipeline.graph import InvoiceSource, PipelineDeps, pipeline_deps, process_invoice
from invoiceiq.repos.app_store import ApprovalStatus
from invoiceiq.shared.cost import summary
from invoiceiq.telemetry import configure_telemetry

ALLOWED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".eml"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@dataclass
class Services:
    """App-lifetime objects, built once in the lifespan and injected into routes."""

    settings: Settings
    pipeline: PipelineDeps
    ask: AskAgent
    events: EventBus
    batches: bool = False  # True when this process runs the durable DBOS batch worker


async def build_services(settings: Settings) -> Services:
    events = EventBus()
    pipeline = await pipeline_deps(settings)
    pipeline.events = events
    # The durable batch worker runs inside the API process. Launching it also recovers any batch
    # that was running when the server last died.
    await start_runtime(settings)
    return Services(
        settings=settings, pipeline=pipeline, ask=build_ask_ap(settings), events=events, batches=True
    )


def create_app(
    settings: Settings | None = None,
    *,
    services_factory: Callable[[Settings], Awaitable[Services]] = build_services,
    services: Services | None = None,
) -> FastAPI:
    """`services=` skips the lifespan entirely (tests); otherwise they are built at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        if getattr(app.state, "services", None) is None:
            s = settings or get_settings()
            configure_telemetry(s)
            app.state.services = await services_factory(s)
        yield
        if app.state.services.batches:
            stop_runtime()

    app = FastAPI(title="InvoiceIQ", version="0.1.0", lifespan=lifespan)
    if services is not None:
        app.state.services = services
    _routes(app)
    return app


def get_services(request: Request) -> Services:
    return request.app.state.services


Svc = Annotated[Services, Depends(get_services)]


class Accepted(BaseModel):
    invoice_id: str
    status: str
    stream: str


class ApprovalItem(BaseModel):
    invoice_id: str
    decision: str
    vendor: str
    invoice_no: str
    amount: str
    reasons: list[str]
    narrative: str | None
    waiting_on: list[dict[str, Any]]


class ApprovalRequest(BaseModel):
    decision: Literal["approve", "deny"]
    note: str | None = None
    amount: Decimal | None = None


class ApprovalResult(BaseModel):
    invoice_id: str
    status: str
    already_resolved: bool
    posting_id: str | None
    amount: Decimal | None
    posted_by: str | None
    message: str | None


class BatchRequest(BaseModel):
    invoice_ids: list[str] | Literal["all"] = "all"


class PathRequest(BaseModel):
    path: str


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


async def _process(svc: Services, source: InvoiceSource) -> None:
    """Background task: run the graph, publish progress; failures are recorded with a category."""
    record = await process_invoice(source, svc.pipeline)
    svc.events.close(source.invoice_id, status=record.status)


def _routes(app: FastAPI) -> None:  # noqa: C901
    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/invoices", status_code=202)
    async def ingest(request: Request, background: BackgroundTasks, svc: Svc) -> Accepted:
        """Multipart upload (field `file`), or JSON `{"path": "data/invoices/inv-001.pdf"}`."""
        store = svc.pipeline.store
        if request.headers.get("content-type", "").startswith("multipart/"):
            form = await request.form()
            upload = form.get("file")
            if not isinstance(upload, UploadFile) or not upload.filename:
                raise HTTPException(422, "multipart field 'file' is required")
            suffix = Path(upload.filename).suffix.lower()
            data = await upload.read()
            if suffix not in ALLOWED_SUFFIXES or len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(422, f"expected a {sorted(ALLOWED_SUFFIXES)} file under 10 MB")
            invoice_id = await store.new_invoice_id(Path(upload.filename).stem)
            path = svc.settings.runtime_dir / "uploads" / f"{invoice_id}{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        else:
            try:
                raw = PathRequest.model_validate_json(await request.body()).path
            except ValidationError as e:
                raise HTTPException(422, 'send multipart "file" or JSON {"path": ...}') from e
            path = (PROJECT_ROOT / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
            # Only files inside the data directory: a path parameter must not read arbitrary files.
            if not path.is_relative_to(svc.settings.data_dir.resolve()) or not path.is_file():
                raise HTTPException(422, f"no such file inside {svc.settings.data_dir}")
            if path.suffix.lower() not in ALLOWED_SUFFIXES:
                raise HTTPException(422, f"expected one of {sorted(ALLOWED_SUFFIXES)}")
            invoice_id = await store.new_invoice_id(path.stem)

        await store.save_invoice(
            InvoiceRecord(invoice_id=invoice_id, file_name=path.name, status="processing")
        )
        svc.events.open(invoice_id)
        background.add_task(_process, svc, InvoiceSource(invoice_id, path))
        return Accepted(invoice_id=invoice_id, status="processing", stream=f"/invoices/{invoice_id}/stream")

    @app.get("/invoices")
    async def list_invoices(svc: Svc, status: str | None = None) -> list[InvoiceRecord]:
        return await svc.pipeline.store.list_invoices(status)

    @app.get("/invoices/{invoice_id}")
    async def get_invoice(invoice_id: str, svc: Svc) -> InvoiceRecord:
        record = await svc.pipeline.store.get_invoice(invoice_id)
        if record is None:
            raise HTTPException(404, f"no invoice {invoice_id}")
        return record

    @app.get("/invoices/{invoice_id}/stream")
    async def stream(invoice_id: str, svc: Svc) -> EventSourceResponse:
        """SSE: graph steps, the extractor's partial JSON, then `done` with the final status."""
        record = await svc.pipeline.store.get_invoice(invoice_id)
        if record is None:
            raise HTTPException(404, f"no invoice {invoice_id}")

        async def events() -> AsyncIterator[dict[str, str]]:
            if not svc.events.known(invoice_id):
                # Processed before this server started: nothing to replay, just the result.
                yield {"event": "done", "data": json.dumps({"type": "done", "status": record.status})}
                return
            async for event in svc.events.subscribe(invoice_id):
                yield {"event": event["type"], "data": json.dumps(event, default=str)}

        return EventSourceResponse(events())

    @app.get("/approvals")
    async def approvals(svc: Svc, status: ApprovalStatus = "pending") -> list[ApprovalItem]:
        store = svc.pipeline.store
        items: list[ApprovalItem] = []
        for p in await store.list_pending(status):
            record = await store.get_invoice(p.invoice_id)
            requests = p.conversation.deferred_tool_requests
            items.append(
                ApprovalItem(
                    invoice_id=p.invoice_id,
                    decision=p.context.decision,
                    vendor=p.context.vendor_name,
                    invoice_no=p.context.invoice_no,
                    amount=f"{p.context.currency} {p.context.amount}",
                    reasons=record.decision.reasons if record and record.decision else [],
                    narrative=record.audit.narrative if record and record.audit else None,
                    waiting_on=[{"tool": c.tool_name, "args": c.args_as_dict()} for c in requests.approvals]
                    if requests
                    else [],
                )
            )
        return items

    @app.post("/approvals/{invoice_id}")
    async def decide(
        invoice_id: str, body: ApprovalRequest, svc: Svc, x_user: Annotated[str, Header(min_length=1)]
    ) -> ApprovalResult:
        d = svc.pipeline
        try:
            r = await resolve(
                invoice_id, approve=body.decision == "approve", user=x_user, note=body.note,
                amount=body.amount,
                poster=d.poster, store=d.store, ledger=d.ledger,
            )  # fmt: skip
        except ApprovalError as e:
            raise HTTPException(404 if str(e).startswith("no pending") else 409, str(e)) from e
        return ApprovalResult(
            invoice_id=r.invoice_id,
            status=r.status,
            already_resolved=r.already_resolved,
            posting_id=r.receipt.posting_id if r.receipt else None,
            amount=r.receipt.amount if r.receipt else None,
            posted_by=r.receipt.posted_by if r.receipt else None,
            message=r.message,
        )

    @app.post("/chat")
    async def chat_route(
        body: ChatRequest, svc: Svc, x_user: Annotated[str, Header()] = "anonymous"
    ) -> ChatReply:
        d = svc.pipeline
        try:
            return await chat(
                body.message, conversation_id=body.conversation_id, user=x_user, agent=svc.ask, store=d.store,
                ledger=d.ledger, today=svc.settings.today(),
            )  # fmt: skip
        except UnknownConversation as e:
            raise HTTPException(404, f"no conversation {e}") from e
        except UsageLimitExceeded as e:
            raise HTTPException(429, f"this conversation has used its budget: {e}") from e

    @app.post("/batches", status_code=202)
    async def start_batch(body: BatchRequest, svc: Svc) -> dict[str, Any]:
        """Start a durable batch over invoice files in data/invoices ("all", or a list of ids)."""
        if not svc.batches:
            raise HTTPException(503, "the durable batch worker isn't running in this process")
        files = sorted((svc.settings.data_dir / "invoices").iterdir())
        if body.invoice_ids != "all":
            unknown = set(body.invoice_ids) - {f.stem for f in files}
            if unknown:
                raise HTTPException(422, f"no such invoice files: {sorted(unknown)}")
            files = [f for f in files if f.stem in body.invoice_ids]
        batch_id = f"batch-{uuid.uuid4().hex[:8]}"
        await submit_batch(batch_id, [(f.stem, f) for f in files])  # returns at once; the queue works
        return {"batch_id": batch_id, "invoices": len(files), "status": f"/batches/{batch_id}"}

    @app.get("/batches/{batch_id}")
    async def get_batch(batch_id: str, svc: Svc) -> dict[str, Any]:
        """The batch workflow's status, and how many of its invoices are in each state."""
        if not svc.batches:
            raise HTTPException(503, "the durable batch worker isn't running in this process")
        status = await batch_status(batch_id)
        if status is None:
            raise HTTPException(404, f"no batch {batch_id}")
        return status

    @app.get("/costs")
    async def costs(svc: Svc) -> dict[str, Any]:
        return summary(path=svc.settings.costs_path, cap_usd=svc.settings.spend_cap_usd)


app = create_app()  # for `uvicorn invoiceiq.api:app`; nothing is built until startup
