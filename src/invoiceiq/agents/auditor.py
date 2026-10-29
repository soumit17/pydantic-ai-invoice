"""Auditor agent (spec §7.2): invoice + deterministic findings in, `AuditReport` out.

Division of labour:
- The deterministic checks (pipeline/checks.py) are facts. The auditor gets them in its prompt and
  is told not to repeat or dispute them.
- The auditor investigates with ledger tools, adds findings the rules can't express (source
  "llm"), and writes the narrative for the approver.
- `merge_findings` combines both lists. The money decision is still `Policy.decide`, not the model.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import Caching, Thinking
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from invoiceiq.capabilities.cost import build_cost_hooks
from invoiceiq.capabilities.fx import fx_capability
from invoiceiq.capabilities.ledger import ledger_capability
from invoiceiq.config import Settings
from invoiceiq.models import AuditReport, Finding, RawInvoice
from invoiceiq.policy import Policy
from invoiceiq.repos.ledger import LedgerRepo, open_ledger

AuditorAgent = Agent["AuditDeps", AuditReport]

AUDIT_LIMITS = UsageLimits(request_limit=6, tool_calls_limit=8)


@dataclass
class AuditDeps:
    ledger: LedgerRepo
    policy: Policy
    today: date


INSTRUCTIONS = """\
You audit vendor invoices for an Indian accounts-payable team, before a human approves payment.

You receive the extracted invoice and the findings of the deterministic checks. Those findings
were computed by code from the ledger: they are facts. Do not repeat them in your findings and do
not argue with them.

1. Investigate with the ledger tools: look up the vendor, fetch the PO if there is one, and search
   the posted invoices if anything suggests a duplicate.
2. Put every check that found nothing wrong in checks_passed, one short line each.
3. findings is only for problems the deterministic checks did not already report, backed by
   concrete evidence from the invoice or a tool result. Never add a finding to say a check passed.
   Most invoices have no findings. What each kind means:
   - exact_duplicate / fuzzy_duplicate: this invoice was already posted (same or reformatted number)
   - price_drift: a unit price above the PO price by more than the threshold
   - unknown_vendor: the supplier is not in the vendor master
   - blocked_vendor: the vendor is blocked or its KYC is pending
   - gst_type_mismatch: IGST on an intra-state supply, or CGST/SGST on an inter-state one
   - math_error: printed figures that don't add up
   - stale_date: older than the stale limit, or dated in the future
   - fx_unverified: an amount in a foreign currency
   Severity: "info" for an observation that needs no action (for example a price exactly at the
   threshold); "warning" when a human should check something; "critical" only for a duplicate
   payment or a blocked vendor. A missing PO is not a finding: mention it in the narrative.
4. Narrative: 2-4 plain sentences for the approver. State each fact once; don't reason aloud.
   Mention every warning or critical finding (the deterministic ones included), the vendor, and
   the total in INR.
5. Suggest a decision: "hold" if any finding is critical, "needs_approval" if any is a warning or
   the total is above the auto-approve limit, otherwise "approve". Policy code makes the final
   decision; yours is advice.
"""


def build_auditor(settings: Settings, *, model: Model | str | None = None) -> AuditorAgent:
    agent: AuditorAgent = Agent(
        model or settings.model_dev,
        deps_type=AuditDeps,
        output_type=AuditReport,
        name="auditor",
        description="Investigates an invoice with ledger tools and explains the findings.",
        instructions=INSTRUCTIONS,
        model_settings=ModelSettings(max_tokens=settings.max_tokens_default),
        retries=2,
        capabilities=[
            # Multi-step runs (tool calls) re-send a growing conversation, so cache messages too.
            Caching(),
            # Off by default, as in the extractor. M6 adds Thinking() per run for hard cases only,
            # which is the point of the escalation experiment.
            Thinking(effort=False),
            build_cost_hooks(settings),
            ledger_capability,  # tools + their usage instructions, always loaded
            fx_capability,  # deferred: only a catalog line until the model loads it
        ],
    )

    @agent.instructions
    def policy_context(ctx: RunContext[AuditDeps]) -> str:
        p = ctx.deps.policy
        return (
            f"Today is {ctx.deps.today:%d %B %Y}. Auto-approve limit: ₹{p.auto_approve_limit_inr}. "
            f"Price drift is flagged above {p.price_drift_pct}% over the PO price. "
            f"Invoices older than {p.stale_after_days} days are stale."
        )

    @agent.output_validator
    def mark_as_llm(ctx: RunContext[AuditDeps], report: AuditReport) -> AuditReport:
        # Deterministic post-processing, not a retry: whatever the model wrote, its findings are
        # LLM-sourced. Trust in a finding depends on where it came from, so the model doesn't get
        # to claim "deterministic".
        findings = [f.model_copy(update={"source": "llm"}) for f in report.findings]
        return report.model_copy(update={"findings": findings})

    return agent


def audit_prompt(invoice: RawInvoice, findings: Sequence[Finding], policy: Policy) -> str:
    invoice_json = invoice.model_dump_json(exclude={"confidence"}, indent=1)
    limit = policy.auto_approve_limit_inr
    if invoice.currency == "INR":
        # Computed in code, so the model states the comparison instead of working it out aloud.
        side = "above" if invoice.total > limit else "within"
        total_line = f"Total: ₹{invoice.total}, {side} the ₹{limit} auto-approve limit."
    else:
        # Left to the model on purpose: converting needs the deferred FX capability.
        total_line = (
            f"Total: {invoice.currency} {invoice.total}. "
            f"Convert it to INR to compare with the ₹{limit} limit."
        )
    findings_json = json.dumps(
        [f.model_dump(include={"kind", "severity", "message", "evidence"}) for f in findings], indent=1
    )
    return (
        f"Invoice:\n{invoice_json}\n\n{total_line}\n\n"
        f"Deterministic findings ({len(findings)}):\n{findings_json if findings else 'none'}"
    )


def merge_findings(deterministic: Sequence[Finding], llm: Sequence[Finding]) -> list[Finding]:
    """Deterministic findings first. An LLM finding of a kind the rules already reported is dropped:
    the rules are authoritative for their kinds."""
    seen = {f.kind for f in deterministic}
    return [*deterministic, *(f for f in llm if f.kind not in seen)]


def audit_deps(settings: Settings) -> AuditDeps:
    return AuditDeps(
        ledger=open_ledger(settings), policy=Policy.from_settings(settings), today=settings.today()
    )
