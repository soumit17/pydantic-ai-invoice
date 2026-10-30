"""Batches: the in-process .map() graph, and (slow) the durable DBOS batch surviving kill -9."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from invoiceiq.config import PROJECT_ROOT
from invoiceiq.pipeline.batch import run_batch_in_process
from invoiceiq.pipeline.graph import InvoiceSource, pipeline_deps
from tests.fakes import fake_batch_auditor, fake_poster, fake_router_extractor, runtime_settings, truth


async def test_map_runs_every_invoice_and_joins_the_records(dataset: Path, tmp_path: Path) -> None:
    settings = runtime_settings(dataset, tmp_path)
    deps = await pipeline_deps(
        settings,
        extractor=fake_router_extractor(settings, dataset),
        auditor=fake_batch_auditor(settings, dataset),
        escalation_auditor=fake_batch_auditor(settings, dataset, thinking="medium"),
        poster=fake_poster(settings),
    )
    files = sorted((dataset / "invoices").iterdir())
    records = await run_batch_in_process([InvoiceSource(p.stem, p) for p in files], deps)
    assert [r.invoice_id for r in records] == [p.stem for p in files]  # sorted, one per invoice
    wrong = {
        r.invoice_id: (r.decision.outcome if r.decision else r.status)
        for r in records
        if (r.decision.outcome if r.decision else None) != truth(dataset, r.invoice_id).expected_decision
    }
    assert wrong == {}
    escalated = [r.invoice_id for r in records if r.audit_escalation]
    assert escalated == ["inv-014"]


async def test_an_empty_batch_is_fine(dataset: Path, tmp_path: Path) -> None:
    deps = await pipeline_deps(
        runtime_settings(dataset, tmp_path), poster=fake_poster(runtime_settings(dataset, tmp_path))
    )
    assert await run_batch_in_process([], deps) == []


@pytest.mark.slow
def test_durable_batch_survives_kill_9(tmp_path: Path) -> None:
    """The real crash test (fake models, $0): a worker is SIGKILLed mid-batch and a new one resumes."""
    command = [sys.executable, "scripts/crash_test.py", "--n", "10", "--kill-after", "4"]
    out = subprocess.run(
        [*command, "--root", str(tmp_path / "crash")],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "PASS" in out.stdout
