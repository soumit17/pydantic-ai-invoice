"""The eval machinery at $0 (M8): datasets, custom evaluators, span capture and the compare gate.

The live evals swap the fake agents for Haiku/Sonnet; everything else here is what they run.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import logfire
import pytest
from logfire.testing import CaptureLogfire
from pydantic_ai import Agent
from pydantic_evals.reporting import EvaluationReport

from evals import audit, extraction
from evals.common import headline
from evals.compare import regressions
from evals.evaluators import LineItemF1
from invoiceiq.config import Settings
from invoiceiq.models import GroundTruth, LineItem, RawInvoice
from tests.fakes import fake_batch_auditor, fake_router_extractor, runtime_settings

IDS = ["inv-001", "inv-020", "inv-024", "inv-028", "inv-040"]  # clean, duplicate, drift, blocked, quotation


@pytest.fixture
def spans(capfire: CaptureLogfire) -> Iterator[None]:
    # What evals.common.setup does via configure_telemetry: without spans, agentic evaluators score 0.
    logfire.instrument_pydantic_ai()
    yield
    Agent.instrument_all(False)


def eval_settings(dataset: Path, tmp_path: Path) -> Settings:
    return runtime_settings(dataset, tmp_path).model_copy(update={"data_dir": dataset})  # read-only use


def truths(dataset: Path) -> list[GroundTruth]:
    return [
        GroundTruth.model_validate_json((dataset / "ground_truth" / f"{i}.json").read_text()) for i in IDS
    ]


async def test_extraction_eval_scores_a_perfect_extractor_100(
    dataset: Path, tmp_path: Path, spans: None
) -> None:
    settings = eval_settings(dataset, tmp_path)
    task = extraction.extraction_task(fake_router_extractor(settings, dataset), settings)
    report: EvaluationReport[Any, Any, Any] = await extraction.build_dataset(truths(dataset)).evaluate(
        task, progress=False
    )
    assert headline(report) == {"field_accuracy": 1.0, "kind_accuracy": 1.0, "task_failures": 0.0}
    for case in report.cases:
        assert case.assertions["SpansCaptured"].value, case.name  # spans reached the evaluators
        assert case.assertions["MaxModelRequests"].value


async def test_audit_eval_reads_the_tool_trail_from_spans(dataset: Path, tmp_path: Path, spans: None) -> None:
    settings = eval_settings(dataset, tmp_path)
    ts = truths(dataset)
    task = audit.audit_task(fake_batch_auditor(settings, dataset), settings, ts)
    report: EvaluationReport[Any, Any, Any] = await audit.build_dataset(ts).evaluate(task, progress=False)
    metrics = headline(report)
    # The rules find everything here, so recall/precision/decisions are perfect...
    assert (metrics["recall"], metrics["precision"], metrics["decision_accuracy"]) == (1.0, 1.0, 1.0)
    assert [c.name for c in report.cases] == ["inv-001", "inv-020", "inv-024", "inv-028"]  # not the quotation
    by_name = {c.name: c for c in report.cases}
    # ...but the fake auditor calls no tools, and the span-based evaluators notice.
    assert by_name["inv-024"].assertions["ToolCorrectness"].value is False  # never looked up the PO
    assert by_name["inv-020"].scores["TrajectoryMatch"].value == 0.0
    assert all(c.assertions["SpansCaptured"].value for c in report.cases)


def line(description: str, qty: str) -> LineItem:
    return LineItem.model_validate({
        "description": description, "hsn_sac": "8471", "qty": qty, "unit_price": "10", "taxable_value": "10",
        "gst_rate": 18, "cgst": "0", "sgst": "0", "igst": "1.8",
    })  # fmt: skip


def test_line_item_f1_matches_by_description_and_quantity(dataset: Path) -> None:
    gt = truths(dataset)[0].expected
    assert isinstance(gt, RawInvoice)
    want = gt.model_copy(
        update={"line_items": [line("Laptop stand, aluminium", "2"), line("USB-C hub", "1")]}
    )
    got = gt.model_copy(update={"line_items": [line("USB C Hub", "1"), line("Laptop stand aluminum", "3")]})

    class Ctx:  # the two fields the evaluator reads
        expected_output, output = want, got

    assert LineItemF1().evaluate(Ctx()) == pytest.approx(0.5)  # type: ignore[arg-type]  # hub matches; stand's qty differs


def test_compare_flags_drops_over_three_points_only() -> None:
    base = {"metrics": {"recall": 0.90, "precision": 0.95, "task_failures": 0}}
    assert regressions(base, {"metrics": {"recall": 0.88, "precision": 0.95, "task_failures": 0}}) == []
    found = regressions(base, {"metrics": {"recall": 0.86, "precision": 0.99, "task_failures": 1}})
    assert found == ["recall: 0.900 -> 0.860 (-4.0 points)", "task_failures: 0 -> 1"]


def test_money_tolerance_is_one_rupee(dataset: Path) -> None:
    from invoiceiq.scoring import score

    gt = truths(dataset)[0].expected
    assert isinstance(gt, RawInvoice)
    off_by_half = gt.model_copy(update={"total": gt.total + Decimal("0.5")})
    assert score(gt, off_by_half).correct == score(gt, gt).correct - 1  # M2's strict 0.01
    assert score(gt, off_by_half, tolerance=Decimal("1")).accuracy == 1.0  # the eval's ±₹1
