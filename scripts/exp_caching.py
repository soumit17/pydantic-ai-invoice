"""LIVE experiment (spec §8.1): what does prompt caching save, and when does it cost more?

    uv run python scripts/exp_caching.py --agent extractor --n 10   # ~$0.02
    uv run python scripts/exp_caching.py --agent auditor --n 10     # ~$0.02

Runs the same N invoices twice: with the agent's Caching capability, then without it. Anthropic
caches nothing unless asked, so "without" is the true baseline. Cache *writes* cost 1.25× normal
input and reads 0.1×, so caching only pays off once a cached prefix is read back enough times.
Numbers come from the cost ledger (runs/costs.jsonl), one line per model request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from invoiceiq.agents.auditor import AUDIT_LIMITS, audit_deps, audit_prompt, build_auditor
from invoiceiq.agents.extractor import EXTRACT_LIMITS, build_extractor, extract_deps
from invoiceiq.config import Settings, get_settings
from invoiceiq.models import GroundTruth, RawInvoice
from invoiceiq.pipeline.checks import run_deterministic_checks
from invoiceiq.pipeline.documents import load_document
from invoiceiq.shared.cost import check_budget
from invoiceiq.telemetry import configure_telemetry


def ledger_rows(settings: Settings) -> list[dict[str, Any]]:
    path = settings.costs_path
    return (
        [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    )


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "requests": len(rows),
        "input": sum(r["input"] for r in rows),
        "cache_read": sum(r["cache_read"] for r in rows),
        "cache_write": sum(r["cache_write"] for r in rows),
        "output": sum(r["output"] for r in rows),
        "usd": sum((Decimal(r["usd"]) for r in rows), Decimal(0)),
    }


async def run_mode(
    settings: Settings, agent_name: str, truths: list[GroundTruth], *, caching: bool
) -> dict[str, Any]:
    before = len(ledger_rows(settings))
    if agent_name == "extractor":
        agent = build_extractor(settings, caching=caching)
        deps = extract_deps(settings)
        for t in truths:
            await agent.run(load_document(settings.data_dir / "invoices" / t.file_name), deps=deps,
                            usage_limits=EXTRACT_LIMITS)  # fmt: skip
    else:
        auditor = build_auditor(settings, caching=caching)
        adeps = audit_deps(settings, seed=True)  # evaluate against the clean seed ledger
        for t in truths:
            assert isinstance(t.expected, RawInvoice)
            rules = await run_deterministic_checks(
                t.expected, ledger=adeps.ledger, policy=adeps.policy, today=adeps.today
            )
            await auditor.run(
                audit_prompt(t.expected, rules, adeps.policy), deps=adeps, usage_limits=AUDIT_LIMITS
            )
    return totals(ledger_rows(settings)[before:])


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=["extractor", "auditor"], default="extractor")
    parser.add_argument("--n", type=int, default=10)
    args = parser.parse_args()

    settings = get_settings()
    configure_telemetry(settings)
    check_budget(path=settings.costs_path, cap_usd=settings.spend_cap_usd)
    truths = [
        gt for p in sorted((settings.data_dir / "ground_truth").glob("*.json"))
        if isinstance((gt := GroundTruth.model_validate_json(p.read_text())).expected, RawInvoice)
    ][: args.n]  # fmt: skip

    print(f"{args.agent}: {len(truths)} invoices, cached first (it pays the cache writes), then uncached\n")
    results = {
        "cached": await run_mode(settings, args.agent, truths, caching=True),
        "uncached": await run_mode(settings, args.agent, truths, caching=False),
    }
    header = (
        f"{'mode':<9} {'requests':>8} {'input':>8} {'cache_read':>10} {'cache_write':>11} "
        f"{'output':>7} {'cost':>10}"
    )
    print(header)
    for mode, r in results.items():
        print(f"{mode:<9} {r['requests']:>8} {r['input']:>8} {r['cache_read']:>10} {r['cache_write']:>11} "
              f"{r['output']:>7} ${r['usd']:>9.5f}")  # fmt: skip
    c, u = results["cached"], results["uncached"]
    saved = u["usd"] - c["usd"]
    share = c["cache_read"] / c["input"] if c["input"] else 0
    verdict = "saved" if saved >= 0 else "COST"
    pct = saved / u["usd"] if u["usd"] else Decimal(0)
    print(f"\n{share:.0%} of cached-mode input tokens were read from the cache;")
    print(f"caching {verdict} ${abs(saved):.5f} ({pct:+.0%}) on this run.")
    print("\njournal row:")
    print(f"| {args.agent} | {len(truths)} | {c['cache_read']} / {c['input']} | {c['cache_write']} | "
          f"${c['usd']:.4f} | ${u['usd']:.4f} | {pct:+.0%} |")  # fmt: skip

    out = settings.runs_dir / f"exp-caching-{args.agent}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"\ndetails: {out.relative_to(settings.runs_dir.parent)}")


if __name__ == "__main__":
    asyncio.run(main())
