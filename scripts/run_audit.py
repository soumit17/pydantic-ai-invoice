"""LIVE: deterministic checks + auditor + policy on the ground-truth invoices (extraction skipped).

    uv run python scripts/run_audit.py --limit 3 -v      # cheap first look: narratives and tool trails
    uv run python scripts/run_audit.py                   # all 39 invoices (M3 acceptance)
    uv run python scripts/run_audit.py --ids inv-038 -v  # one invoice in detail
    uv run python scripts/run_audit.py --console         # also print spans (agent → model → tools)

Acceptance (spec M3): finding recall ≥ 0.85, every duplicate caught, FX loaded only for USD invoices.
Each run writes runs/audit-<time>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime
from typing import Any

from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart

from invoiceiq.agents.auditor import AUDIT_LIMITS, audit_deps, audit_prompt, build_auditor, merge_findings
from invoiceiq.capabilities.fx import FX_ID
from invoiceiq.config import get_settings
from invoiceiq.models import GroundTruth, RawInvoice
from invoiceiq.pipeline.checks import run_deterministic_checks
from invoiceiq.policy import to_inr
from invoiceiq.shared.cost import check_budget, total_spend
from invoiceiq.telemetry import configure_telemetry

DUPLICATES = {"exact_duplicate", "fuzzy_duplicate"}


def trajectory(messages: list[ModelMessage]) -> list[str]:
    """Tool calls in order, e.g. ['get_vendor', 'get_po', 'load_capability(fx)', 'convert_to_inr']."""
    calls: list[str] = []
    for m in messages:
        if isinstance(m, ModelResponse):
            for p in m.parts:
                if isinstance(p, ToolCallPart) and p.tool_name != "final_result":
                    arg = p.args_as_dict().get("id") if p.tool_name == "load_capability" else None
                    calls.append(f"{p.tool_name}({arg})" if arg else p.tool_name)
    return calls


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--final", action="store_true", help="use MODEL_FINAL (Sonnet) instead of Haiku")
    parser.add_argument("--console", action="store_true", help="print spans to the terminal")
    parser.add_argument("-v", "--verbose", action="store_true", help="narrative, tool trail and LLM findings")
    args = parser.parse_args()

    settings = get_settings()
    configure_telemetry(settings, console=args.console)
    spent_before = check_budget(path=settings.costs_path, cap_usd=settings.spend_cap_usd)

    truths = [
        gt
        for p in sorted((settings.data_dir / "ground_truth").glob("*.json"))
        if isinstance((gt := GroundTruth.model_validate_json(p.read_text())).expected, RawInvoice)
    ]
    if args.ids:
        truths = [t for t in truths if t.file_id in args.ids]
    if args.limit:
        truths = truths[: args.limit]

    model = settings.model_final if args.final else settings.model_dev
    agent = build_auditor(settings, model=model)
    deps = audit_deps(settings)
    print(f"auditing {len(truths)} invoice(s) with {model}; spent so far ${spent_before:.4f}\n")

    rows: list[dict[str, Any]] = []
    for t in truths:
        inv = t.expected
        assert isinstance(inv, RawInvoice)
        rule_findings = await run_deterministic_checks(
            inv, ledger=deps.ledger, policy=deps.policy, today=deps.today
        )
        start = time.perf_counter()
        try:
            result = await agent.run(
                audit_prompt(inv, rule_findings, deps.policy), deps=deps, usage_limits=AUDIT_LIMITS
            )
        except (UnexpectedModelBehavior, UsageLimitExceeded) as e:
            print(f"ERR  {t.file_id} {t.category_name:<24} {type(e).__name__}: {e}")
            rows.append(
                {"file_id": t.file_id, "category": t.category_name, "error": f"{type(e).__name__}: {e}"}
            )
            continue
        seconds = time.perf_counter() - start
        report = result.output
        merged = merge_findings(rule_findings, report.findings)
        decision = deps.policy.decide(merged, total_inr=to_inr(inv.total, inv.currency))

        expected = {f.kind for f in t.expected_findings}
        flagged = {f.kind for f in merged if f.severity != "info"}  # info = observation, not a flag
        trail = trajectory(result.all_messages())
        fx_loaded = f"load_capability({FX_ID})" in trail
        ok = flagged == expected and decision.outcome == t.expected_decision
        print(
            f"{'OK ' if ok else 'BAD'}  {t.file_id} {t.category_name:<24} {decision.outcome:<18} "
            f"llm+{len(report.findings)}  tools {len(trail)}{'  FX' if fx_loaded else ''}  "
            f"req {result.usage.requests} in {result.usage.input_tokens:>6} "
            f"out {result.usage.output_tokens:>5}  "
            f"{seconds:4.1f}s"
        )
        if not ok:
            print(
                f"       expected {sorted(expected) or '-'} → {t.expected_decision}; "
                f"got {sorted(flagged) or '-'}"
            )
        if args.verbose:
            print(f"       tools: {' → '.join(trail) or '(none)'}")
            for c in report.checks_passed:
                print(f"       ✓ {c}")
            for f in report.findings:
                print(f"       + llm {f.severity} {f.kind}: {f.message}")
            print(f"       narrative: {report.narrative}")
            print(f"       auditor suggests: {report.suggested_decision}")
        rows.append({
            "file_id": t.file_id, "category": t.category_name, "currency": inv.currency,
            "expected": sorted(expected), "flagged": sorted(flagged),
            "llm_findings": [f.model_dump(mode="json") for f in report.findings],
            "checks_passed": report.checks_passed,
            "decision": decision.outcome, "expected_decision": t.expected_decision,
            "suggested": report.suggested_decision, "narrative": report.narrative,
            "trajectory": trail, "fx_loaded": fx_loaded, "requests": result.usage.requests,
            "input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens,
            "cache_read_tokens": result.usage.cache_read_tokens, "seconds": round(seconds, 2),
        })  # fmt: skip

    done = [r for r in rows if "error" not in r]
    true_pos = sum(len(set(r["expected"]) & set(r["flagged"])) for r in done)
    want = sum(len(r["expected"]) for r in done)
    got = sum(len(r["flagged"]) for r in done)
    dup_rows = [r for r in done if DUPLICATES & set(r["expected"])]
    dups_caught = sum(set(r["expected"]) <= set(r["flagged"]) for r in dup_rows)
    false_pos = [(r["file_id"], k) for r in done for k in set(r["flagged"]) - set(r["expected"])]
    fx_rows = {r["file_id"] for r in done if r["fx_loaded"]}
    usd_rows = {r["file_id"] for r in done if r["currency"] != "INR"}
    decisions_ok = sum(r["decision"] == r["expected_decision"] for r in done)
    advice_ok = sum(
        {"approve": "auto_approved", "needs_approval": "awaiting_approval", "hold": "held"}[r["suggested"]]
        == r["decision"]
        for r in done
    )
    spent = total_spend(path=settings.costs_path) - spent_before

    print(f"\nfinding recall:     {true_pos}/{want} = {true_pos / max(want, 1):.2f}   (need ≥ 0.85)")
    print(f"finding precision:  {true_pos}/{got} = {true_pos / max(got, 1):.2f}   (info findings excluded)")
    print(f"false positives:    {false_pos or 'none'}")
    print(f"duplicates caught:  {dups_caught}/{len(dup_rows)}   (need all)")
    print(f"decision matches:   {decisions_ok}/{len(done)}")
    print(f"auditor advice = policy: {advice_ok}/{len(done)}")
    print(f"FX loaded on:       {sorted(fx_rows) or 'none'}  (USD invoices: {sorted(usd_rows) or 'none'})"
          f"  → {'OK' if fx_rows == usd_rows else 'MISMATCH'}")  # fmt: skip
    print(f"errors:             {len(rows) - len(done)}")
    print(f"cost:               ${spent:.4f} (${spent / max(len(done), 1):.5f} per invoice)")

    out = settings.runs_dir / f"audit-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": model, "cost_usd": str(spent), "rows": rows}, indent=2, default=str))
    print(f"details:            {out.relative_to(settings.runs_dir.parent)}")


if __name__ == "__main__":
    asyncio.run(main())
