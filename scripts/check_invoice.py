"""$0: run the deterministic checks and the policy on ground-truth invoices, and show each step.

uv run python scripts/check_invoice.py inv-027          # one invoice, in detail
uv run python scripts/check_invoice.py --all            # one line per invoice
"""

from __future__ import annotations

import asyncio
import sys

from invoiceiq.config import get_settings
from invoiceiq.models import GroundTruth, RawInvoice
from invoiceiq.pipeline.checks import run_deterministic_checks
from invoiceiq.policy import Policy, to_inr
from invoiceiq.repos.ledger import LedgerRepo


async def check(gt: GroundTruth, *, verbose: bool) -> bool:
    settings = get_settings()
    policy = Policy.from_settings(settings)
    if isinstance(gt.expected, RawInvoice):
        inv = gt.expected
        findings = await run_deterministic_checks(
            inv, ledger=LedgerRepo(settings.ledger_path), policy=policy, today=settings.today()
        )
        decision = policy.decide(findings, total_inr=to_inr(inv.total, inv.currency))
    else:
        findings = []
        decision = policy.decide([], failure=gt.expected)

    ok = decision.outcome == gt.expected_decision and sorted(
        (f.kind, f.severity) for f in findings
    ) == sorted((f.kind, f.severity) for f in gt.expected_findings)
    if not verbose:
        kinds = ",".join(f.kind for f in findings) or "-"
        print(f"{'OK ' if ok else 'BAD'} {gt.file_id} {gt.category_name:<24} {decision.outcome:<18} {kinds}")
        return ok

    print(f"{gt.file_id}  category {gt.category} ({gt.category_name}), {gt.file_name}")
    if gt.notes:
        print(f"  note: {gt.notes}")
    if isinstance(gt.expected, RawInvoice):
        inv = gt.expected
        print(f"  {inv.supplier.name} [{inv.supplier.gstin}]  {inv.invoice_no}  {inv.invoice_date}")
        print(f"  PO {inv.po_number}  {inv.currency} {inv.total}  ({len(inv.line_items)} lines)")
    print("  findings:")
    for f in findings:
        print(f"    - {f.severity:<8} {f.kind}: {f.message}")
        for k, v in f.evidence.items():
            print(f"        {k} = {v}")
    if not findings:
        print("    (none)")
    print(f"  decision: {decision.outcome}")
    for r in decision.reasons:
        print(f"    because {r}")
    print(f"  expected: {gt.expected_decision}  →  {'MATCH' if ok else 'MISMATCH'}")
    return ok


async def main() -> None:
    settings = get_settings()
    truth_dir = settings.data_dir / "ground_truth"
    if not truth_dir.exists():
        sys.exit("No data yet. Run: uv run python scripts/gen_mockdata.py")
    if len(sys.argv) > 1 and sys.argv[1] != "--all":
        path = truth_dir / f"{sys.argv[1]}.json"
        await check(GroundTruth.model_validate_json(path.read_text()), verbose=True)
        return
    results = [
        await check(GroundTruth.model_validate_json(p.read_text()), verbose=False)
        for p in sorted(truth_dir.glob("*.json"))
    ]
    print(f"\n{sum(results)}/{len(results)} match the ground truth")


if __name__ == "__main__":
    asyncio.run(main())
