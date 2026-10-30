"""What every eval script shares: flags, the span check, cost, and the results file compare.py reads."""

from __future__ import annotations

import argparse
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic_evals import Dataset
from pydantic_evals.reporting import EvaluationReport
from pydantic_evals.reporting.analyses import ScalarResult

from invoiceiq.config import Settings, get_settings
from invoiceiq.models import GroundTruth
from invoiceiq.shared.cost import check_budget, total_spend
from invoiceiq.telemetry import configure_telemetry


def parse_args(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--limit", type=int, help="first N cases only (cheap iteration)")
    parser.add_argument("--ids", nargs="*", help="only these file ids")
    parser.add_argument("--final", action="store_true", help="MODEL_FINAL (Sonnet) instead of Haiku")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("-v", "--verbose", action="store_true", help="print every case's scores")
    return parser.parse_args()


def setup(args: argparse.Namespace) -> tuple[Settings, str, Decimal]:
    settings = get_settings()
    # The span-based evaluators read spans captured in-process. Without this they all score 0 silently.
    configure_telemetry(settings)
    spent = check_budget(path=settings.costs_path, cap_usd=settings.spend_cap_usd)
    return settings, settings.model_final if args.final else settings.model_dev, spent


def ground_truths(settings: Settings, args: argparse.Namespace) -> list[GroundTruth]:
    truths = [
        GroundTruth.model_validate_json(p.read_text())
        for p in sorted((settings.data_dir / "ground_truth").glob("*.json"))
    ]
    if args.ids:
        truths = [t for t in truths if t.file_id in args.ids]
    return truths[: args.limit] if args.limit else truths


def headline(report: EvaluationReport[Any, Any, Any]) -> dict[str, float]:
    return {a.title: float(a.value) for a in report.analyses if isinstance(a, ScalarResult)}


async def evaluate(
    dataset: Dataset[Any, Any, Any],
    task: Callable[[Any], Awaitable[Any]],
    *,
    settings: Settings,
    model: str,
    spent_before: Decimal,
    args: argparse.Namespace,
) -> dict[str, float]:
    print(f"{dataset.name}: {len(dataset.cases)} case(s) on {model}; spent so far ${spent_before:.4f}\n")
    report = await dataset.evaluate(task, name=f"{dataset.name}-{model}", max_concurrency=args.concurrency)
    report.print(include_input=False, include_output=False, include_durations=True, include_averages=True)
    for analysis in report.analyses:
        if not isinstance(analysis, ScalarResult):
            print(f"\n{analysis.title}")
            for row in getattr(analysis, "rows", []):
                print("  ", row)
    if args.verbose:
        for case in report.cases:
            print(case.name, {k: v.value for k, v in {**case.scores, **case.assertions}.items()})
    for failure in report.failures:
        print(f"FAILED {failure.name}: {failure.error_message[:200]}")

    # Spec §17.2: prove the spans arrived, or the agentic scores mean nothing.
    missing = [
        c.name
        for c in report.cases
        if "SpansCaptured" in c.assertions and not c.assertions["SpansCaptured"].value
    ]
    if missing:
        raise SystemExit(
            f"no spans captured for {missing}: is Logfire configured? (agentic scores would be 0)"
        )

    metrics = headline(report)
    spent = total_spend(path=settings.costs_path) - spent_before
    print(f"\nmetrics: {json.dumps({k: round(v, 4) for k, v in metrics.items()})}")
    print(f"cost: ${spent:.4f}")
    save(settings, dataset.name or "eval", model, metrics, spent, report)
    return metrics


def results_dir(settings: Settings) -> Path:
    return settings.runs_dir / "evals"


def save(
    settings: Settings,
    name: str,
    model: str,
    metrics: dict[str, float],
    spent: Decimal,
    report: EvaluationReport[Any, Any, Any],
) -> None:
    """runs/evals/<name>-<model>-<time>.json, plus latest-<name>-<model>.json for compare.py."""
    out = results_dir(settings)
    out.mkdir(parents=True, exist_ok=True)
    short = model.split(":")[-1]
    body = {
        "dataset": name,
        "model": model,
        "time": datetime.now().isoformat(timespec="seconds"),
        "cases": len(report.cases) + len(report.failures),
        "metrics": metrics,
        "cost_usd": str(spent),
        "per_case": {
            c.name: {k: v.value for k, v in {**c.scores, **c.assertions, **c.labels}.items()}
            for c in report.cases
        },
        "failures": {f.name: f.error_message for f in report.failures},
    }
    text = json.dumps(body, indent=1, default=str)
    (out / f"{name}-{short}-{datetime.now():%Y%m%d-%H%M%S}.json").write_text(text)
    (out / f"latest-{name}-{short}.json").write_text(text)
    print(f"saved runs/evals/latest-{name}-{short}.json")
