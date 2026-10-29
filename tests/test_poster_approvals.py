"""Deferred-tool approvals: approve, approve twice, deny, edit the amount, held needs a note, restart."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from invoiceiq.approvals import ApprovalError, Resolution, resolve
from invoiceiq.config import PROJECT_ROOT
from invoiceiq.pipeline.graph import InvoiceSource, PipelineDeps, pipeline_deps, run_pipeline
from tests.fakes import PosterCalls, fake_auditor, fake_extractor, fake_poster, runtime_settings, truth

HIGH_VALUE = "inv-017"  # clean, above ₹50k → awaiting_approval
HELD = "inv-028"  # blocked vendor → held


async def parked(
    dataset: Path, tmp_path: Path, file_id: str, calls: PosterCalls | None = None
) -> PipelineDeps:
    gt = truth(dataset, file_id)
    settings = runtime_settings(dataset, tmp_path)
    deps = await pipeline_deps(
        settings, extractor=fake_extractor(settings, gt.expected), auditor=fake_auditor(settings),
        poster=fake_poster(settings, calls),
    )  # fmt: skip
    record = await run_pipeline(InvoiceSource(file_id, dataset / "invoices" / gt.file_name), deps)
    assert record.status in ("awaiting_approval", "held")
    return deps


async def approve(deps: PipelineDeps, file_id: str, **kw: object) -> Resolution:
    return await resolve(file_id, poster=deps.poster, store=deps.store, ledger=deps.ledger, **kw)  # pyright: ignore[reportArgumentType]


def ledger_rows(deps: PipelineDeps, key: str) -> list[tuple[str, str, str]]:
    with sqlite3.connect(deps.ledger.path) as db:
        return db.execute(
            "SELECT posting_id, total, posted_by FROM posted_invoices WHERE idempotency_key = ?", (key,)
        ).fetchall()


async def test_approve_posts_with_the_approvers_identity(dataset: Path, tmp_path: Path) -> None:
    deps = await parked(dataset, tmp_path, HIGH_VALUE)
    r = await approve(deps, HIGH_VALUE, approve=True, user="alice")
    assert r.status == "posted" and r.receipt is not None and r.receipt.posted_by == "alice"
    pending = await deps.store.get_pending(HIGH_VALUE)
    assert pending is not None and pending.status == "approved" and pending.resolved_by == "alice"
    stored = await deps.store.get_invoice(HIGH_VALUE)
    assert stored is not None and stored.status == "posted" and stored.receipt == r.receipt


async def test_approving_twice_posts_once(dataset: Path, tmp_path: Path) -> None:
    calls = PosterCalls()
    deps = await parked(dataset, tmp_path, HIGH_VALUE, calls)
    first = await approve(deps, HIGH_VALUE, approve=True, user="alice")
    requests_after_first = calls.requests
    second = await approve(deps, HIGH_VALUE, approve=True, user="bob")
    assert second.already_resolved and second.receipt == first.receipt  # bob gets alice's posting
    assert calls.requests == requests_after_first  # no second model call
    assert first.receipt is not None and len(ledger_rows(deps, first.receipt.idempotency_key)) == 1


async def test_deny_returns_tool_denied_to_the_model_and_posts_nothing(dataset: Path, tmp_path: Path) -> None:
    calls = PosterCalls()
    deps = await parked(dataset, tmp_path, HIGH_VALUE, calls)
    pending = await deps.store.get_pending(HIGH_VALUE)
    assert pending is not None
    r = await approve(deps, HIGH_VALUE, approve=False, user="alice", note="Wrong cost centre")
    assert r.status == "denied" and r.receipt is None
    assert calls.seen_outcomes == ["denied"] and r.message is not None
    assert "An approver (alice) denied this posting: Wrong cost centre" in r.message  # not a "ledger error"
    assert ledger_rows(deps, pending.context.idempotency_key) == []


async def test_approve_with_an_edited_amount_posts_the_partial_amount(dataset: Path, tmp_path: Path) -> None:
    deps = await parked(dataset, tmp_path, HIGH_VALUE)
    r = await approve(deps, HIGH_VALUE, approve=True, user="alice", amount=Decimal("100000.00"))
    assert r.receipt is not None and r.receipt.amount == Decimal(
        "100000.00"
    )  # ToolApproved(override_args=...)


async def test_amount_above_the_invoice_total_is_refused(dataset: Path, tmp_path: Path) -> None:
    deps = await parked(dataset, tmp_path, HIGH_VALUE)
    with pytest.raises(ApprovalError, match="at most"):
        await approve(deps, HIGH_VALUE, approve=True, user="alice", amount=Decimal("99999999"))


async def test_held_invoices_need_a_note(dataset: Path, tmp_path: Path) -> None:
    deps = await parked(dataset, tmp_path, HELD)
    with pytest.raises(ApprovalError, match="note"):
        await approve(deps, HELD, approve=True, user="alice")
    r = await approve(
        deps, HELD, approve=True, user="alice", note="Vendor unblocked by procurement on 28 Oct"
    )
    assert r.status == "posted"


async def test_unknown_invoice(dataset: Path, tmp_path: Path) -> None:
    deps = await parked(dataset, tmp_path, HIGH_VALUE)
    with pytest.raises(ApprovalError, match="no pending approval"):
        await approve(deps, "inv-999", approve=True, user="alice")


def _phase(phase: str, dataset: Path, root: Path) -> dict[str, object]:
    out = subprocess.run(
        [sys.executable, "-m", "tests.restart_helper", phase, str(dataset), str(root)],
        cwd=PROJECT_ROOT, check=True, capture_output=True, text=True,
    )  # fmt: skip
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_pending_approval_survives_a_restart(dataset: Path, tmp_path: Path) -> None:
    parked_run = _phase("park", dataset, tmp_path)
    approved = _phase("approve", dataset, tmp_path)  # a brand-new process: nothing in memory
    again = _phase("approve", dataset, tmp_path)  # and a third one approving again
    assert parked_run["status"] == "awaiting_approval"
    assert approved["status"] == "posted" and approved["pid"] != parked_run["pid"]
    assert again["status"] == "posted" and again["already"] is True
    with sqlite3.connect(tmp_path / "data" / "runtime" / "ledger.sqlite") as db:
        (count,) = db.execute("SELECT count(*) FROM posted_invoices WHERE posted_by = 'alice'").fetchone()
    assert count == 1


def test_posting_request_explains_the_internal_id() -> None:
    # A live run refused to post because "inv-027" didn't match vendor invoice "MDP/0259/26-27".
    from datetime import date

    from invoiceiq.agents.poster import INSTRUCTIONS, PostingContext, posting_request

    ctx = PostingContext(
        invoice_ref="inv-027", vendor_id="V002", vendor_name="Maa Durga", invoice_no="MDP/0259/26-27",
        invoice_date=date(2026, 9, 26), amount=Decimal("16528.59"), currency="INR", content_hash="k",
        idempotency_key="k", decision="auto_approved",
    )  # fmt: skip
    assert "Post our document inv-027: vendor invoice MDP/0259/26-27" in posting_request(ctx)
    assert "internal document id" in INSTRUCTIONS and "not a reason to stop" in INSTRUCTIONS
