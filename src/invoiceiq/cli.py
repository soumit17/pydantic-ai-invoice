"""The `invoiceiq` command line (spec §13). Runs in-process, through the same services as the API.

invoiceiq serve                                  # the HTTP API on :8000
invoiceiq ingest data/invoices/inv-017.pdf       # LIVE: one file through the pipeline
invoiceiq run --fresh                            # LIVE: all 40, compared with ground truth
invoiceiq pending                                # $0: the approver inbox
invoiceiq approve inv-017 --user alice [--note ...] [--amount ...]
invoiceiq deny inv-029 --user alice --note "unknown supplier"
invoiceiq chat --user alice                      # LIVE: Ask-AP, with memory
invoiceiq postings | costs | reset               # $0
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import time
from collections.abc import Coroutine
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import typer

from invoiceiq.agents.ask_ap import UnknownConversation, build_ask_ap, chat
from invoiceiq.approvals import ApprovalError, resolve
from invoiceiq.config import Settings, get_settings
from invoiceiq.models import GroundTruth, InvoiceRecord
from invoiceiq.pipeline.graph import InvoiceSource, PipelineDeps, pipeline_deps, run_pipeline
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
            record = await run_pipeline(InvoiceSource(invoice_id, path), deps)
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
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
    console: Console = False,
) -> None:
    """LIVE: run the mock invoices and compare each decision with the ground truth."""

    async def go() -> None:
        settings = _setup(console)
        spent_before = check_budget(path=settings.costs_path, cap_usd=settings.spend_cap_usd)
        files = sorted((settings.data_dir / "invoices").iterdir())
        if ids:
            files = [f for f in files if f.stem in ids]
        if fresh:
            shutil.rmtree(settings.runtime_dir, ignore_errors=True)
        elif (n := _postings_count(settings)) > 0:
            typer.echo(f"note: the runtime ledger already holds {n} posting(s) from earlier runs; re-running")
            typer.echo(
                "      those invoices will (correctly) be held as duplicates. Use --fresh to start clean.\n"
            )
        deps = await pipeline_deps(settings)
        typer.echo(f"running {len(files)} invoice(s) through the graph; spent so far ${spent_before:.4f}\n")
        matches = 0
        for path in files:
            start = time.perf_counter()
            try:
                record = await run_pipeline(InvoiceSource(path.stem, path), deps)
            except Exception as e:  # M7 adds proper error handling; here, report and carry on
                typer.echo(f"ERR  {path.stem}  {type(e).__name__}: {e}")
                continue
            gt_path = settings.data_dir / "ground_truth" / f"{path.stem}.json"
            expected = (
                GroundTruth.model_validate_json(gt_path.read_text()).expected_decision
                if gt_path.exists()
                else None
            )
            matches += _print_record(record, expected=expected, seconds=time.perf_counter() - start)
            if verbose and record.audit:
                typer.echo(f"       narrative: {record.audit.narrative}")
        typer.echo(f"\ndecisions matching ground truth: {matches}/{len(files)}")
        typer.echo(f"cost: ${total_spend(path=settings.costs_path) - spent_before:.4f}")

    _run(go())


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
    console: Console = False,
) -> None:
    """LIVE: talk to Ask-AP. The conversation is stored after every turn; resume it with --conversation."""

    async def go() -> None:
        settings = _setup(console)
        deps = await pipeline_deps(settings)
        agent = build_ask_ap(settings)
        conversation_id = conversation
        typer.echo(
            "Ask AP. Empty line to quit." + (f" Continuing {conversation_id}." if conversation_id else "")
        )
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
