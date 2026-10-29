"""Run the full pipeline graph and act as the approver. (The typer CLI in M5 replaces this.)

    uv run python scripts/pipeline.py run --ids inv-001 inv-017 inv-028 -v   # LIVE: ~$0.002/invoice
    uv run python scripts/pipeline.py run --fresh                             # LIVE: all 40, clean ledger
    uv run python scripts/pipeline.py pending                                 # $0: the approver inbox
    uv run python scripts/pipeline.py approve inv-017 --user alice            # LIVE: resumes the poster
    uv run python scripts/pipeline.py approve inv-028 --user alice --note "vendor unblocked"
    uv run python scripts/pipeline.py approve inv-017 --user alice --amount 100000   # partial posting
    uv run python scripts/pipeline.py deny inv-029 --user alice --note "unknown supplier"
    uv run python scripts/pipeline.py postings                                # $0: what reached the ledger
    uv run python scripts/pipeline.py reset                                   # $0: delete data/runtime/

Add --console to `run`, `approve` or `deny` to watch the spans: graph steps, agent runs, tools.
State lives in data/runtime/ (git-ignored); the committed data/ledger.sqlite is never written.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sqlite3
import time
from decimal import Decimal

from invoiceiq.approvals import ApprovalError, resolve
from invoiceiq.config import Settings, get_settings
from invoiceiq.models import GroundTruth
from invoiceiq.pipeline.graph import InvoiceSource, pipeline_deps, run_pipeline
from invoiceiq.repos.app_store import AppStore
from invoiceiq.shared.cost import check_budget, total_spend
from invoiceiq.telemetry import configure_telemetry


async def cmd_run(settings: Settings, args: argparse.Namespace) -> None:
    spent_before = check_budget(path=settings.costs_path, cap_usd=settings.spend_cap_usd)
    files = sorted((settings.data_dir / "invoices").iterdir())
    if args.ids:
        files = [f for f in files if f.stem in args.ids]
    if args.limit:
        files = files[: args.limit]
    if args.fresh:
        shutil.rmtree(settings.runtime_dir, ignore_errors=True)
    elif (n := pipeline_postings(settings)) > 0:
        print(f"note: the runtime ledger already holds {n} posting(s) from earlier runs, so re-running")
        print("      those invoices will (correctly) be held as duplicates. Use --fresh to start clean.\n")
    deps = await pipeline_deps(settings)
    print(f"running {len(files)} invoice(s) through the graph; spent so far ${spent_before:.4f}\n")
    matches = 0
    for path in files:
        start = time.perf_counter()
        try:
            record = await run_pipeline(InvoiceSource(path.stem, path), deps)
        except Exception as e:  # M7 adds proper error handling; here, report and carry on
            print(f"ERR  {path.stem}  {type(e).__name__}: {e}")
            continue
        gt_path = settings.data_dir / "ground_truth" / f"{path.stem}.json"
        expected = (
            GroundTruth.model_validate_json(gt_path.read_text()).expected_decision
            if gt_path.exists()
            else None
        )
        outcome = record.decision.outcome if record.decision else "-"
        ok = expected is None or outcome == expected
        matches += ok
        # Info findings are observations: shown in brackets, they never change the decision.
        kinds = ",".join(f.kind if f.severity != "info" else f"({f.kind})" for f in record.findings) or "-"
        print(
            f"{'OK ' if ok else 'BAD'}  {path.stem}  {record.status:<18} {kinds:<32} "
            f"{time.perf_counter() - start:4.1f}s" + ("" if ok else f"   (expected {expected})")
        )
        if args.verbose:
            if record.audit:
                print(f"       narrative: {record.audit.narrative}")
            if record.receipt:
                print(f"       posted {record.receipt.posting_id} by {record.receipt.posted_by}")
    print(f"\ndecisions matching ground truth: {matches}/{len(files)}")
    print(f"cost: ${total_spend(path=settings.costs_path) - spent_before:.4f}")


async def cmd_pending(settings: Settings, args: argparse.Namespace) -> None:
    store = await AppStore.open(settings)
    pending = await store.list_pending()
    if not pending:
        print("no pending approvals")
    for p in pending:
        record = await store.get_invoice(p.invoice_id)
        c = p.context
        print(f"{p.invoice_id}  {c.decision:<18} {c.vendor_name} {c.invoice_no}  {c.currency} {c.amount}")
        if record and record.decision:
            for reason in record.decision.reasons:
                print(f"       {reason}")
        if record and record.audit:
            print(f"       narrative: {record.audit.narrative}")
        # The paused tool call, straight out of the stored Conversation.
        requests = p.conversation.deferred_tool_requests
        for call in requests.approvals if requests else []:
            print(f"       waiting on: {call.tool_name}({call.args_as_dict()})")


async def cmd_resolve(settings: Settings, args: argparse.Namespace, *, approve: bool) -> None:
    deps = await pipeline_deps(settings)
    try:
        r = await resolve(
            args.invoice_id,
            approve=approve,
            user=args.user,
            note=args.note,
            amount=Decimal(args.amount) if getattr(args, "amount", None) else None,
            poster=deps.poster,
            store=deps.store,
            ledger=deps.ledger,
        )
    except ApprovalError as e:
        raise SystemExit(f"refused: {e}") from e
    print(
        f"{r.invoice_id}: {r.status}"
        + (" (already resolved earlier; nothing new posted)" if r.already_resolved else "")
    )
    if r.receipt:
        print(
            f"  {r.receipt.posting_id}  amount {r.receipt.amount}  "
            f"by {r.receipt.posted_by} at {r.receipt.posted_at}"
        )
    if r.message:
        print(f"  poster: {r.message}")


def pipeline_postings(settings: Settings) -> int:
    if not settings.ledger_path.exists():
        return 0
    with sqlite3.connect(settings.ledger_path) as db:
        (n,) = db.execute("SELECT count(*) FROM posted_invoices WHERE posted_by != 'seed'").fetchone()
    return int(n)


def cmd_postings(settings: Settings) -> None:
    if not settings.ledger_path.exists():
        print("no runtime ledger yet")
        return
    with sqlite3.connect(settings.ledger_path) as db:
        rows = db.execute(
            "SELECT posting_id, vendor_id, invoice_no, total, currency, posted_by, posted_at "
            "FROM posted_invoices WHERE posted_by != 'seed' ORDER BY posted_at"
        ).fetchall()
    print(f"{len(rows)} posting(s) made by the pipeline:")
    for row in rows:
        print("  " + "  ".join(str(x) for x in row))


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--ids", nargs="*")
    run.add_argument("--limit", type=int)
    run.add_argument("-v", "--verbose", action="store_true")
    run.add_argument("--console", action="store_true")
    run.add_argument("--fresh", action="store_true", help="reset data/runtime/ first (a clean ledger)")
    sub.add_parser("pending")
    for name in ("approve", "deny"):
        p = sub.add_parser(name)
        p.add_argument("invoice_id")
        p.add_argument("--user", required=True)
        p.add_argument("--note")
        p.add_argument("--console", action="store_true")
        if name == "approve":
            p.add_argument("--amount", help="approve a partial amount (edits the tool call's arguments)")
    sub.add_parser("postings")
    sub.add_parser("reset")
    args = parser.parse_args()

    settings = get_settings()
    if args.cmd == "reset":
        shutil.rmtree(settings.runtime_dir, ignore_errors=True)
        print(f"deleted {settings.runtime_dir} (the committed seed ledger is untouched)")
        return
    if args.cmd == "postings":
        cmd_postings(settings)
        return
    configure_telemetry(settings, console=getattr(args, "console", False))
    if args.cmd == "run":
        await cmd_run(settings, args)
    elif args.cmd == "pending":
        await cmd_pending(settings, args)
    else:
        await cmd_resolve(settings, args, approve=args.cmd == "approve")


if __name__ == "__main__":
    asyncio.run(main())
