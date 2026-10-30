"""The `invoiceiq` command line (spec §13). Runs in-process, through the same services as the API.

Commands (LIVE = calls the model and costs money; the rest are $0):
- serve: the HTTP API on :8000. watch ID: follow its SSE stream for one invoice.
- ingest FILE..., run [--fresh] [--parallel]: LIVE, the pipeline on files, or on all 40 vs ground truth.
- batch [IDS] [--batch-id ID] [--fresh]: LIVE, a durable DBOS batch that survives kill -9 and resumes.
- pending, approve ID --user U [--note N] [--amount A], deny ID --user U --note N: approvals.
- chat --user U [--mcp-url URL | --local]: LIVE, Ask-AP with memory; the ledger over MCP.
- mcp [--http HOST:PORT]: the ledger MCP server, over stdio (Claude Code) or streamable HTTP.
- postings, costs, reset: housekeeping.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import time
from collections.abc import Coroutine
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import typer

from invoiceiq.agents.ask_ap import UnknownConversation, build_ask_ap, chat
from invoiceiq.approvals import ApprovalError, resolve
from invoiceiq.config import Settings, get_settings
from invoiceiq.models import GroundTruth, InvoiceRecord
from invoiceiq.pipeline.batch import run_batch_in_process, start_runtime, stop_runtime, submit_batch
from invoiceiq.pipeline.graph import InvoiceSource, PipelineDeps, pipeline_deps, process_invoice
from invoiceiq.repos.app_store import AppStore
from invoiceiq.shared.cost import check_budget, summary, total_spend
from invoiceiq.telemetry import configure_telemetry

app = typer.Typer(help="InvoiceIQ: GST invoice intelligence on Pydantic AI.", no_args_is_help=True)

Console = Annotated[bool, typer.Option("--console", help="Print spans (graph steps, agent runs, tools).")]
User = Annotated[str, typer.Option("--user", help="Who is acting (recorded on postings).")]


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _setup(console: bool = False) -> Settings:
    settings = get_settings()
    configure_telemetry(settings, console=console)
    return settings


def _print_record(
    record: InvoiceRecord, *, expected: str | None = None, seconds: float | None = None
) -> bool:
    outcome = record.decision.outcome if record.decision else "-"
    ok = expected is None or outcome == expected
    # Info findings are observations: shown in brackets, they never change the decision.
    kinds = ",".join(f.kind if f.severity != "info" else f"({f.kind})" for f in record.findings) or "-"
    timing = f"{seconds:4.1f}s" if seconds is not None else ""
    tail = "" if ok else f"   (expected {expected})"
    typer.echo(
        f"{'OK ' if ok else 'BAD'}  {record.invoice_id}  {record.status:<18} {kinds:<32} {timing}{tail}"
    )
    return ok


def _postings_count(settings: Settings) -> int:
    if not settings.ledger_path.exists():
        return 0
    with sqlite3.connect(settings.ledger_path) as db:
        (n,) = db.execute("SELECT count(*) FROM posted_invoices WHERE posted_by != 'seed'").fetchone()
    return int(n)


# --- server -------------------------------------------------------------------------------------


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the HTTP API."""
    import uvicorn

    uvicorn.run("invoiceiq.api:app", host=host, port=port)


@app.command()
def mcp(
    http: Annotated[
        str | None,
        typer.Option(help="Serve streamable HTTP on HOST:PORT (e.g. 127.0.0.1:8765) instead of stdio."),
    ] = None,
) -> None:
    """Run the ledger MCP server: stdio for Claude Code, or streamable HTTP at http://HOST:PORT/mcp."""
    from invoiceiq.mcp_server import build_mcp_server

    settings = get_settings()
    server = build_mcp_server(settings)
    if http is None:
        server.run(transport="stdio")  # stdout is the protocol channel: print nothing else
        return
    host, _, port = http.rpartition(":")
    typer.echo(f"ledger MCP server on http://{host or '127.0.0.1'}:{port}/mcp", err=True)
    server.run(transport="streamable-http", host=host or "127.0.0.1", port=int(port))


# --- pipeline -----------------------------------------------------------------------------------


@app.command()
def ingest(
    files: Annotated[list[Path], typer.Argument(help="Invoice files (pdf, png, jpg, eml).")],
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
    console: Console = False,
) -> None:
    """LIVE: run files through the full pipeline (extract → checks → audit → decide → post/park)."""

    async def go() -> None:
        settings = _setup(console)
        check_budget(path=settings.costs_path, cap_usd=settings.spend_cap_usd)
        deps = await pipeline_deps(settings)
        for path in files:
            if not path.is_file():
                typer.echo(f"ERR  {path}: no such file")
                continue
            invoice_id = await deps.store.new_invoice_id(path.stem)
            start = time.perf_counter()
            record = await process_invoice(InvoiceSource(invoice_id, path), deps)
            _print_record(record, seconds=time.perf_counter() - start)
            if verbose and record.audit:
                typer.echo(f"       narrative: {record.audit.narrative}")

    _run(go())


@app.command("run")
def run_all(
    ids: Annotated[
        list[str] | None, typer.Argument(help="Invoice ids (default: all in data/invoices).")
    ] = None,
    fresh: Annotated[
        bool, typer.Option("--fresh", help="Reset data/runtime/ first (a clean ledger).")
    ] = False,
    parallel: Annotated[
        bool, typer.Option("--parallel", help="All at once via the .map() batch graph (model calls capped).")
    ] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
    console: Console = False,
) -> None:
    """LIVE: run the mock invoices in-process and compare each decision with the ground truth."""

    async def go() -> None:
        settings = _setup(console)
        spent_before = check_budget(path=settings.costs_path, cap_usd=settings.spend_cap_usd)
        files = _select(settings, ids)
        _prepare_runtime(settings, fresh)
        deps = await pipeline_deps(settings)
        typer.echo(f"running {len(files)} invoice(s) through the graph; spent so far ${spent_before:.4f}\n")
        started, matches = time.perf_counter(), 0
        if parallel:
            for record in await run_batch_in_process([InvoiceSource(p.stem, p) for p in files], deps):
                matches += _report(settings, record, None, verbose)
        else:
            for path in files:
                start = time.perf_counter()
                record = await process_invoice(InvoiceSource(path.stem, path), deps)
                matches += _report(settings, record, time.perf_counter() - start, verbose)
        typer.echo(f"\ndecisions matching ground truth: {matches}/{len(files)}")
        typer.echo(f"time: {time.perf_counter() - started:.1f}s")
        typer.echo(f"cost: ${total_spend(path=settings.costs_path) - spent_before:.4f}")

    _run(go())


@app.command()
def batch(
    ids: Annotated[
        list[str] | None, typer.Argument(help="Invoice ids (default: all in data/invoices).")
    ] = None,
    batch_id: Annotated[
        str | None, typer.Option("--batch-id", help="Reuse an id to resume that batch after a crash.")
    ] = None,
    fresh: Annotated[
        bool, typer.Option("--fresh", help="Reset data/runtime/ first (ledger, DBOS state).")
    ] = False,
    console: Console = False,
) -> None:
    """LIVE: a durable batch (DBOS). kill -9 it any time; run it again with the same --batch-id and it
    resumes: finished invoices aren't redone, and nothing is posted twice."""

    async def go() -> None:
        settings = _setup(console)
        spent_before = check_budget(path=settings.costs_path, cap_usd=settings.spend_cap_usd)
        files = _select(settings, ids)
        _prepare_runtime(settings, fresh)
        bid = batch_id or f"batch-{datetime.now():%Y%m%d-%H%M%S}"
        await start_runtime(settings)  # launch() also recovers this batch if it crashed earlier
        typer.echo(f"batch {bid}: {len(files)} invoice(s), durable (resume with --batch-id {bid})\n")
        started = time.perf_counter()
        try:
            handle = await submit_batch(bid, [(p.stem, p) for p in files])
            results = await handle.get_result()
        finally:
            stop_runtime()
        store = await AppStore.open(settings)
        matches = 0
        for item in results:
            record = await store.get_invoice(item["invoice_id"])
            if record is not None:
                matches += _report(settings, record, None, False)
        typer.echo(f"\ndecisions matching ground truth: {matches}/{len(results)}")
        typer.echo(f"time: {time.perf_counter() - started:.1f}s")
        typer.echo(f"cost: ${total_spend(path=settings.costs_path) - spent_before:.4f}")

    _run(go())


def _select(settings: Settings, ids: list[str] | None) -> list[Path]:
    files = sorted((settings.data_dir / "invoices").iterdir())
    return [f for f in files if f.stem in ids] if ids else files


def _prepare_runtime(settings: Settings, fresh: bool) -> None:
    if fresh:
        shutil.rmtree(settings.runtime_dir, ignore_errors=True)
    elif (n := _postings_count(settings)) > 0:
        typer.echo(f"note: the runtime ledger already holds {n} posting(s) from earlier runs; re-running")
        typer.echo(
            "      those invoices will (correctly) be held as duplicates. Use --fresh to start clean.\n"
        )


def _report(settings: Settings, record: InvoiceRecord, seconds: float | None, verbose: bool) -> bool:
    """Print one invoice against its ground truth; True if the decision matches."""
    if record.status == "failed":
        typer.echo(f"ERR  {record.invoice_id}  {record.error}")
        return False
    gt_path = settings.data_dir / "ground_truth" / f"{record.invoice_id}.json"
    expected = (
        GroundTruth.model_validate_json(gt_path.read_text()).expected_decision if gt_path.exists() else None
    )
    ok = _print_record(record, expected=expected, seconds=seconds)
    if verbose and record.audit:
        typer.echo(f"       narrative: {record.audit.narrative}")
    return ok


@app.command()
def watch(
    invoice_id: str,
    server: Annotated[
        str, typer.Option(help="Base URL of a running `invoiceiq serve`.")
    ] = "http://127.0.0.1:8000",
) -> None:
    """Follow /invoices/{id}/stream on a running server: graph steps and the partial JSON growing."""
    import httpx

    n = 0
    with httpx.stream("GET", f"{server}/invoices/{invoice_id}/stream", timeout=None) as response:
        if response.status_code != 200:
            typer.echo(f"{response.status_code}: {response.read().decode()}", err=True)
            raise typer.Exit(1)
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            event: dict[str, Any] = json.loads(line[5:])
            kind = event["type"]
            if kind == "step":
                typer.echo(f"step     {event['node']}")
            elif kind == "partial":
                n += 1
                invoice: dict[str, Any] = event["invoice"]
                lines: list[Any] = invoice.get("line_items") or []
                fields = ", ".join(k if k != "line_items" else f"line_items({len(lines)})" for k in invoice)
                typer.echo(
                    f"partial  #{n} attempt {event['attempt']}: {len(json.dumps(invoice)):>5} chars  {fields}"
                )
            else:
                typer.echo(f"{kind:<8} {json.dumps({k: v for k, v in event.items() if k != 'type'})}")


# --- approvals ----------------------------------------------------------------------------------


@app.command()
def pending() -> None:
    """The approver inbox: every posting waiting for a human."""

    async def go() -> None:
        deps = await pipeline_deps(_setup())
        items = await deps.store.list_pending()
        if not items:
            typer.echo("no pending approvals")
        for p in items:
            record = await deps.store.get_invoice(p.invoice_id)
            c = p.context
            typer.echo(
                f"{p.invoice_id}  {c.decision:<18} {c.vendor_name} {c.invoice_no}  {c.currency} {c.amount}"
            )
            for reason in record.decision.reasons if record and record.decision else []:
                typer.echo(f"       {reason}")
            if record and record.audit:
                typer.echo(f"       narrative: {record.audit.narrative}")
            requests = p.conversation.deferred_tool_requests
            for call in requests.approvals if requests else []:
                typer.echo(f"       waiting on: {call.tool_name}({call.args_as_dict()})")

    _run(go())


async def _resolve(deps: PipelineDeps, invoice_id: str, **kw: Any) -> None:
    try:
        r = await resolve(invoice_id, poster=deps.poster, store=deps.store, ledger=deps.ledger, **kw)
    except ApprovalError as e:
        typer.echo(f"refused: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(
        f"{r.invoice_id}: {r.status}"
        + (" (already resolved earlier; nothing new posted)" if r.already_resolved else "")
    )
    if r.receipt:
        typer.echo(
            f"  {r.receipt.posting_id}  amount {r.receipt.amount}  "
            f"by {r.receipt.posted_by} at {r.receipt.posted_at}"
        )
    if r.message:
        typer.echo(f"  poster: {r.message}")


@app.command()
def approve(
    invoice_id: str,
    user: User,
    note: Annotated[str | None, typer.Option(help="Required for held invoices.")] = None,
    amount: Annotated[
        str | None, typer.Option(help="Approve a partial amount (edits the tool call's arguments).")
    ] = None,
    console: Console = False,
) -> None:
    """LIVE: approve a pending posting; the paused poster run resumes and posts."""

    async def go() -> None:
        deps = await pipeline_deps(_setup(console))
        await _resolve(
            deps, invoice_id, approve=True, user=user, note=note, amount=Decimal(amount) if amount else None
        )

    _run(go())


@app.command()
def deny(
    invoice_id: str,
    user: User,
    note: Annotated[str | None, typer.Option(help="The reason; the poster relays it.")] = None,
    console: Console = False,
) -> None:
    """LIVE: deny a pending posting; the poster sees ToolDenied and nothing is posted."""

    async def go() -> None:
        deps = await pipeline_deps(_setup(console))
        await _resolve(deps, invoice_id, approve=False, user=user, note=note)

    _run(go())


# --- Ask-AP -------------------------------------------------------------------------------------


@app.command("chat")
def chat_cmd(
    user: User = "anonymous",
    conversation: Annotated[str | None, typer.Option(help="Continue a stored conversation by id.")] = None,
    mcp_url: Annotated[
        str | None,
        typer.Option(help="A running MCP server, e.g. http://127.0.0.1:8765/mcp (default: in-process)."),
    ] = None,
    local: Annotated[bool, typer.Option("--local", help="Direct ledger tools instead of MCP.")] = False,
    console: Console = False,
) -> None:
    """LIVE: talk to Ask-AP. The conversation is stored after every turn; resume it with --conversation."""

    async def go() -> None:
        settings = _setup(console)
        if mcp_url:
            settings = settings.model_copy(update={"mcp_url": mcp_url})
        deps = await pipeline_deps(settings)
        agent = build_ask_ap(settings, ledger="local" if local else "mcp")
        mode = "direct tools" if local else f"MCP ({settings.mcp_url or 'in-process server'})"
        conversation_id = conversation
        resuming = f" Continuing {conversation_id}." if conversation_id else ""
        typer.echo(f"Ask AP, ledger via {mode}. Empty line to quit.{resuming}")
        while True:
            message = typer.prompt("you", default="", show_default=False).strip()
            if not message:
                break
            try:
                reply = await chat(
                    message, conversation_id=conversation_id, user=user, agent=agent, store=deps.store,
                    ledger=deps.ledger, today=settings.today(),
                )  # fmt: skip
            except UnknownConversation as e:
                typer.echo(f"no conversation {e}", err=True)
                raise typer.Exit(1) from e
            conversation_id = reply.conversation_id
            typer.echo(
                f"ask-ap: {reply.reply}\n        [{reply.requests_used}/{reply.requests_limit} requests used]"
            )
        if conversation_id:
            typer.echo(f"conversation id: {conversation_id}")

    _run(go())


# --- housekeeping -------------------------------------------------------------------------------


@app.command()
def postings() -> None:
    """What the pipeline has posted to the (runtime) ledger."""
    settings = get_settings()
    if not settings.ledger_path.exists():
        typer.echo("no runtime ledger yet")
        return
    with sqlite3.connect(settings.ledger_path) as db:
        rows = db.execute(
            "SELECT posting_id, vendor_id, invoice_no, total, currency, posted_by, posted_at "
            "FROM posted_invoices WHERE posted_by != 'seed' ORDER BY posted_at"
        ).fetchall()
    typer.echo(f"{len(rows)} posting(s) made by the pipeline:")
    for row in rows:
        typer.echo("  " + "  ".join(str(x) for x in row))


@app.command()
def costs() -> None:
    """Spend so far against the cap, by agent and by model."""
    settings = get_settings()
    s = summary(path=settings.costs_path, cap_usd=settings.spend_cap_usd)
    typer.echo(
        f"spent ${s['total_usd']} of ${s['cap_usd']} ({s['requests']} requests); ${s['remaining_usd']} left"
    )
    for label, usd in s["by_label"].items():
        typer.echo(f"  {label:<14} ${usd}")


@app.command()
def reset() -> None:
    """Delete data/runtime/ (working ledger, app.sqlite, uploads). The committed seed is untouched."""
    settings = get_settings()
    shutil.rmtree(settings.runtime_dir, ignore_errors=True)
    typer.echo(f"deleted {settings.runtime_dir}")
