"""Indian GST rules (spec §4.1), as plain functions.

Synthetic-data rules for the lab, not tax advice. Both the strict `Invoice` model and the
deterministic `strict_validate` check call these, so there is one source of truth.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from invoiceiq.models import RawInvoice

GSTIN_PATTERN = r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
_GSTIN_RE = re.compile(GSTIN_PATTERN)

STATES: dict[str, str] = {
    "07": "Delhi",
    "09": "Uttar Pradesh",
    "19": "West Bengal",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "27": "Maharashtra",
    "29": "Karnataka",
    "33": "Tamil Nadu",
}

GST_RATES = (0, 5, 12, 18, 28)
TOLERANCE = Decimal("1.00")  # ±₹1 rounding allowance on every comparison
PAISE = Decimal("0.01")


def is_valid_gstin(value: str) -> bool:
    return _GSTIN_RE.fullmatch(value) is not None


def state_code(gstin: str) -> str:
    return gstin[:2]


def money(value: Decimal) -> Decimal:
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


def financial_year(d: date) -> str:
    """Indian FY runs April to March: 2026-10-29 is in '2026-27'."""
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def is_intra_state(supplier_gstin: str, buyer_gstin: str) -> bool:
    return state_code(supplier_gstin) == state_code(buyer_gstin)


def line_tax(taxable: Decimal, rate: int, *, intra: bool) -> tuple[Decimal, Decimal, Decimal]:
    """(cgst, sgst, igst) for one line. Intra-state splits the rate equally into CGST and SGST."""
    if intra:
        half = money(taxable * rate / 200)
        return half, half, Decimal(0)
    return Decimal(0), Decimal(0), money(taxable * rate / 100)


def arithmetic_issues(inv: RawInvoice) -> list[str]:
    """Every arithmetic inconsistency in the invoice as printed, each as a readable sentence."""
    issues: list[str] = []
    for i, line in enumerate(inv.line_items, start=1):
        expected = line.qty * line.unit_price
        if abs(expected - line.taxable_value) > TOLERANCE:
            issues.append(
                f"line {i} ({line.description}): qty × unit price = {money(expected)}, "
                f"but taxable value is {line.taxable_value}"
            )
        if line.gst_rate is not None:
            charged = line.cgst + line.sgst + line.igst
            expected_tax = line.taxable_value * line.gst_rate / 100
            if abs(charged - expected_tax) > TOLERANCE:
                issues.append(
                    f"line {i} ({line.description}): tax charged {charged}, "
                    f"but {line.gst_rate}% of {line.taxable_value} is {money(expected_tax)}"
                )

    subtotal = sum((li.taxable_value for li in inv.line_items), Decimal(0))
    tax = sum((li.cgst + li.sgst + li.igst for li in inv.line_items), Decimal(0))
    if abs(subtotal - inv.subtotal) > TOLERANCE:
        issues.append(f"subtotal is {inv.subtotal}, but the lines sum to {subtotal}")
    if abs(tax - inv.total_tax) > TOLERANCE:
        issues.append(f"total tax is {inv.total_tax}, but line taxes sum to {tax}")
    if abs(inv.subtotal + inv.total_tax - inv.total) > TOLERANCE:
        issues.append(f"total is {inv.total}, but subtotal + tax = {inv.subtotal + inv.total_tax}")
    return issues


def gst_type_issues(inv: RawInvoice) -> list[str]:
    """CGST+SGST on an inter-state supply, or IGST on an intra-state one."""
    if inv.currency != "INR" or inv.supplier.gstin is None or inv.buyer.gstin is None:
        return []
    supplier_state = state_code(inv.supplier.gstin)
    if is_intra_state(inv.supplier.gstin, inv.buyer.gstin):
        if any(li.igst > 0 for li in inv.line_items):
            return [f"IGST charged on an intra-state supply (both parties in state {supplier_state})"]
    elif any(li.cgst > 0 or li.sgst > 0 for li in inv.line_items):
        return [
            f"CGST/SGST charged on an inter-state supply "
            f"(supplier state {supplier_state}, buyer state {state_code(inv.buyer.gstin)})"
        ]
    return []
