"""LIVE: the README's results table. All 40 invoices through the pipeline, one at a time.

    uv run python scripts/benchmark.py            # Haiku, ~$0.06, ~6 min
    uv run python scripts/benchmark.py --final    # Sonnet as the main model (~$1.30); optional

One at a time so each latency is one invoice's own (no queueing behind the others). Resets
data/runtime/ first, like `invoiceiq run --fresh`. Cost per invoice comes from the cost ledger:
lines carry the invoice id (run metadata, M7); the guards' and judge's lines don't, so they are
added to the total and spread evenly. Writes runs/benchmark-<model>-<time>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

from invoiceiq.config import get_settings
from invoiceiq.models import GroundTruth
from invoiceiq.pipeline.graph import InvoiceSource, pipeline_deps, process_invoice
from invoiceiq.shared.cost import check_budget
from invoiceiq.telemetry import configure_telemetry


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile: p95 of 40 values is the 38th smallest."""
    ordered = sorted(values)
    return ordered[max(0, round(p / 100 * len(ordered)) - 1)]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--final", action="store_true", help="MODEL_FINAL (Sonnet) as the main model")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    settings = get_settings()
    if args.final:
        settings = settings.model_copy(update={"model_dev": settings.model_final})
    configure_telemetry(settings)
    check_budget(path=settings.costs_path, cap_usd=settings.spend_cap_usd)
    shutil.rmtree(settings.runtime_dir, ignore_errors=True)  # a clean ledger, as `run --fresh`
    files = sorted((settings.data_dir / "invoices").iterdir())[: args.limit]
    deps = await pipeline_deps(settings)
    ledger_lines_before = (
        len(settings.costs_path.read_text().splitlines()) if settings.costs_path.exists() else 0
    )
    print(f"benchmark: {len(files)} invoices, one at a time, main model {settings.model_dev}\n")

    rows: list[dict[str, Any]] = []
    for path in files:
        start = time.perf_counter()
        record = await process_invoice(InvoiceSource(path.stem, path), deps)
        seconds = time.perf_counter() - start
        truth = GroundTruth.model_validate_json(
            (settings.data_dir / "ground_truth" / f"{path.stem}.json").read_text()
        )
        outcome = (
            record.decision.outcome
            if record.decision
            else ("rejected" if record.status == "rejected" else None)
        )
        rows.append({"invoice": path.stem, "seconds": seconds, "status": record.status,
                     "correct": outcome == truth.expected_decision})  # fmt: skip
        print(f"{'OK ' if rows[-1]['correct'] else 'BAD'} {path.stem}  {record.status:<18} {seconds:5.1f}s")

    lines = [json.loads(line) for line in settings.costs_path.read_text().splitlines()[ledger_lines_before:]]
    total = sum((Decimal(ln["usd"]) for ln in lines), Decimal(0))
    unattributed = sum((Decimal(ln["usd"]) for ln in lines if not ln.get("invoice")), Decimal(0))
    by_agent: dict[str, Decimal] = {}
    for ln in lines:
        by_agent[ln["label"]] = by_agent.get(ln["label"], Decimal(0)) + Decimal(ln["usd"])
    latencies = [r["seconds"] for r in rows]
    result = {
        "model": settings.model_dev,
        "time": datetime.now().isoformat(timespec="seconds"),
        "invoices": len(rows),
        "decisions_correct": sum(r["correct"] for r in rows),
        "cost_total_usd": str(total),
        "cost_per_invoice_usd": str(total / len(rows)),
        "cost_unattributed_usd": str(unattributed),
        "cost_by_agent_usd": {k: str(v) for k, v in sorted(by_agent.items())},
        "latency_p50_s": round(statistics.median(latencies), 2),
        "latency_p95_s": round(percentile(latencies, 95), 2),
        "latency_max_s": round(max(latencies), 2),
        "rows": rows,
    }
    out = (
        settings.runs_dir
        / f"benchmark-{settings.model_dev.split(':')[-1]}-{datetime.now():%Y%m%d-%H%M%S}.json"
    )
    out.write_text(json.dumps(result, indent=1))

    print("\n| model | decisions | cost / invoice | p50 | p95 |\n|---|---|---|---|---|")
    model = settings.model_dev.split(":")[-1]
    p50, p95, ok = result["latency_p50_s"], result["latency_p95_s"], result["decisions_correct"]
    print(f"| {model} | {ok}/{len(rows)} | ${total / len(rows):.4f} | {p50}s | {p95}s |")
    print(f"\ncost by agent: {json.dumps(result['cost_by_agent_usd'])}")
    print(f"saved {out.relative_to(settings.runs_dir.parent)}")


if __name__ == "__main__":
    asyncio.run(main())
