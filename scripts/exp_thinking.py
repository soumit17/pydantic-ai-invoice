"""LIVE experiment (spec §8.6): does Thinking fix the auditor's false alarms, and at what cost?

    uv run python scripts/exp_thinking.py                     # 4 hard cases × 3 repeats × 3 modes, ~$0.05
    uv run python scripts/exp_thinking.py --repeats 1 -v      # cheaper, with each run's findings

Modes, on the same audits:
    off         no thinking (how M3/M4 ran)
    escalation  production rule: think only when the auditor disagrees with the rules (run_audit)
    always      Thinking(effort="medium") on every audit

The default cases are the ones the auditor got wrong in live runs: inv-014 (IGST false alarm),
inv-027 (exactly +5.00% drift), inv-019 (a "critical" for a passed check, M3) and inv-039
(unknown_vendor on a known foreign vendor, M3). Repeats show how stable each mode is.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic_ai.capabilities import Thinking
from pydantic_ai.settings import ModelSettings

from invoiceiq.agents.auditor import (
    AUDIT_LIMITS,
    ESCALATION_MAX_TOKENS,
    audit_deps,
    audit_prompt,
    build_auditor,
    merge_findings,
    run_audit,
)
from invoiceiq.config import Settings, get_settings
from invoiceiq.models import GroundTruth, RawInvoice
from invoiceiq.pipeline.checks import run_deterministic_checks
from invoiceiq.policy import to_inr
from invoiceiq.shared.cost import check_budget
from invoiceiq.telemetry import configure_telemetry

HARD_CASES = ["inv-014", "inv-027", "inv-019", "inv-039"]
MODES = ["off", "escalation", "always"]


def ledger_rows(settings: Settings) -> list[dict[str, Any]]:
    path = settings.costs_path
    return (
        [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", nargs="*", default=HARD_CASES)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    configure_telemetry(settings)
    check_budget(path=settings.costs_path, cap_usd=settings.spend_cap_usd)
    auditor = build_auditor(settings)
    deps = audit_deps(settings, seed=True)  # evaluate against the clean seed ledger

    stats: dict[str, dict[str, Any]] = {
        m: {"audits": 0, "false_pos": 0, "decisions_ok": 0, "escalated": 0, "reasoning": 0, "output": 0,
            "usd": Decimal(0)} for m in MODES
    }  # fmt: skip
    runs: list[dict[str, Any]] = []
    for case in args.cases:
        gt = GroundTruth.model_validate_json(
            (settings.data_dir / "ground_truth" / f"{case}.json").read_text()
        )
        assert isinstance(gt.expected, RawInvoice)
        inv = gt.expected
        rules = await run_deterministic_checks(inv, ledger=deps.ledger, policy=deps.policy, today=deps.today)
        expected = {f.kind for f in gt.expected_findings}
        for repeat in range(args.repeats):
            for mode in MODES:
                before = len(ledger_rows(settings))
                escalation: str | None = None
                if mode == "escalation":
                    outcome = await run_audit(auditor, inv, rules, deps, escalate=True)
                    report, escalation = outcome.report, outcome.escalation
                else:
                    extra: dict[str, Any] = {}
                    if mode == "always":
                        extra = {
                            "capabilities": [Thinking(effort="medium")],
                            "model_settings": ModelSettings(max_tokens=ESCALATION_MAX_TOKENS),
                        }
                    result = await auditor.run(
                        audit_prompt(inv, rules, deps.policy), deps=deps, usage_limits=AUDIT_LIMITS, **extra
                    )
                    report = result.output
                rows = ledger_rows(settings)[before:]
                merged = merge_findings(rules, report.findings)
                decision = deps.policy.decide(merged, total_inr=to_inr(inv.total, inv.currency))
                flagged = {f.kind for f in merged if f.severity != "info"}
                fp = sorted(flagged - expected)
                s = stats[mode]
                s["audits"] += 1
                s["false_pos"] += len(fp)
                s["decisions_ok"] += decision.outcome == gt.expected_decision
                s["escalated"] += escalation is not None
                s["reasoning"] += sum(r.get("reasoning", 0) for r in rows)
                s["output"] += sum(r["output"] for r in rows)
                s["usd"] += sum((Decimal(r["usd"]) for r in rows), Decimal(0))
                runs.append(
                    {
                        "case": case, "repeat": repeat, "mode": mode, "false_positives": fp,
                        "decision": decision.outcome, "expected": gt.expected_decision,
                        "escalation": escalation,
                        "llm_findings": [f.model_dump(mode="json") for f in report.findings],
                        "narrative": report.narrative,
                    }
                )  # fmt: skip
                print(f"{case} #{repeat + 1} {mode:<10} {decision.outcome:<18} FP {fp or '-'}"
                      + (f"  [escalated: {escalation}]" if escalation else ""))  # fmt: skip
                if args.verbose:
                    for f in report.findings:
                        print(f"           + {f.severity} {f.kind}: {f.message[:110]}")
    print(f"\n{'mode':<11} {'audits':>6} {'false pos':>9} {'decisions':>9} {'escalated':>9} "
          f"{'reasoning tok':>13} {'output tok':>10} {'$/audit':>9}")  # fmt: skip
    for mode, s in stats.items():
        n = max(s["audits"], 1)
        decisions = f"{s['decisions_ok']}/{s['audits']}"
        print(
            f"{mode:<11} {s['audits']:>6} {s['false_pos']:>9} {decisions:>9} {s['escalated']:>9} "
            f"{s['reasoning'] // n:>13} {s['output'] // n:>10} ${s['usd'] / n:>8.5f}"
        )
    out = settings.runs_dir / f"exp-thinking-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"stats": stats, "runs": runs}, indent=2, default=str))
    print(f"\ndetails: {out.relative_to(settings.runs_dir.parent)}")


if __name__ == "__main__":
    asyncio.run(main())
