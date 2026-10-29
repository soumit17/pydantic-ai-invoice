"""M4 acceptance: every invoice category reaches its expected decision through the real graph."""

from __future__ import annotations

from pathlib import Path

import pytest

from invoiceiq.models import RawInvoice
from invoiceiq.pipeline.graph import PIPELINE_DOC, InvoiceSource, pipeline_deps, render_doc, run_pipeline
from tests.fakes import fake_auditor, fake_extractor, fake_poster, runtime_settings, truth

FILE_IDS = [f"inv-{i:03d}" for i in range(1, 41)]
STATUS_FOR = {
    "auto_approved": "posted",
    "awaiting_approval": "awaiting_approval",
    "held": "held",
    "rejected": "rejected",
}


@pytest.mark.parametrize("file_id", FILE_IDS)
async def test_every_invoice_reaches_its_expected_decision(
    dataset: Path, tmp_path: Path, file_id: str
) -> None:
    gt = truth(dataset, file_id)
    settings = runtime_settings(dataset, tmp_path)
    deps = await pipeline_deps(
        settings,
        extractor=fake_extractor(settings, gt.expected),
        auditor=fake_auditor(settings),
        poster=fake_poster(settings),
    )
    record = await run_pipeline(InvoiceSource(file_id, dataset / "invoices" / gt.file_name), deps)

    assert record.decision is not None and record.decision.outcome == gt.expected_decision
    assert record.status == STATUS_FOR[gt.expected_decision]
    assert sorted(f.kind for f in record.findings) == sorted(f.kind for f in gt.expected_findings)
    assert await deps.store.get_invoice(file_id) == record  # the stored record is the graph's output

    pending = await deps.store.get_pending(file_id)
    if gt.expected_decision == "auto_approved":
        assert record.receipt is not None and record.receipt.posted_by == "policy"
        assert pending is not None and pending.status == "approved"  # same path as a human approval
    elif gt.expected_decision == "rejected":
        assert pending is None and record.receipt is None
    else:
        assert pending is not None and pending.status == "pending" and record.receipt is None


async def test_reprocessing_a_posted_invoice_is_held_as_a_duplicate(dataset: Path, tmp_path: Path) -> None:
    gt = truth(dataset, "inv-001")
    settings = runtime_settings(dataset, tmp_path)
    assert isinstance(gt.expected, RawInvoice)
    deps = await pipeline_deps(
        settings, extractor=fake_extractor(settings, gt.expected), auditor=fake_auditor(settings),
        poster=fake_poster(settings),
    )  # fmt: skip
    path = dataset / "invoices" / gt.file_name
    first = await run_pipeline(InvoiceSource("upload-1", path), deps)
    second = await run_pipeline(InvoiceSource("upload-2", path), deps)  # same file, new upload id
    assert first.status == "posted"
    assert second.status == "held" and [f.kind for f in second.findings] == ["exact_duplicate"]


def test_pipeline_doc_is_up_to_date() -> None:
    assert PIPELINE_DOC.read_text() == render_doc(), "run: uv run python -m invoiceiq.pipeline.graph"
