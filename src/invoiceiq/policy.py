"""The money decision (spec §4.2): pure Python, deterministic, unit-tested.

The LLM may explain a decision, but never makes it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from invoiceiq.config import Settings
from invoiceiq.gst import money
from invoiceiq.models import Decision, ExtractionFailure, Finding

# Static mock rates, also used by FxCapability (M3). Not real market data.
FX_RATES_INR: dict[str, Decimal] = {"INR": Decimal(1), "USD": Decimal("84.00"), "EUR": Decimal("91.00")}


def to_inr(amount: Decimal, currency: str) -> Decimal:
    return money(amount * FX_RATES_INR[currency])


@dataclass(frozen=True)
class Policy:
    auto_approve_limit_inr: Decimal = Decimal(50_000)
    price_drift_pct: Decimal = Decimal(5)
    stale_after_days: int = 180

    @classmethod
    def from_settings(cls, settings: Settings) -> Policy:
        return cls(
            auto_approve_limit_inr=Decimal(settings.auto_approve_limit_inr),
            price_drift_pct=Decimal(str(settings.price_drift_pct)),
        )

    def decide(
        self,
        findings: Sequence[Finding],
        *,
        total_inr: Decimal | None = None,
        failure: ExtractionFailure | None = None,
    ) -> Decision:
        """Apply the policy table top to bottom; the first matching row wins."""
        if failure is not None:
            reasons = [f"extraction failed: {failure.reason}"]
            if failure.detected_document_type:
                reasons.append(f"document looks like a {failure.detected_document_type}")
            if failure.missing_fields:
                reasons.append("missing: " + ", ".join(failure.missing_fields))
            return Decision(outcome="rejected", reasons=reasons)

        critical = [f for f in findings if f.severity == "critical"]
        if critical:
            return Decision(outcome="held", reasons=[f"critical: {f.kind}: {f.message}" for f in critical])

        warnings = [f for f in findings if f.severity == "warning"]
        if warnings:
            return Decision(
                outcome="awaiting_approval",
                reasons=[f"warning: {f.kind}: {f.message}" for f in warnings],
            )

        if total_inr is None:
            raise ValueError("total_inr is required when there is no extraction failure")
        if total_inr > self.auto_approve_limit_inr:
            return Decision(
                outcome="awaiting_approval",
                reasons=[
                    f"total ₹{total_inr} is above the auto-approve limit ₹{self.auto_approve_limit_inr}"
                ],
            )
        return Decision(
            outcome="auto_approved",
            reasons=[f"no findings and total ₹{total_inr} is within ₹{self.auto_approve_limit_inr}"],
        )
