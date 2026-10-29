"""Deterministic checks (spec §9): no LLM anywhere in this file.

Each `check_*` function is pure: it takes the invoice plus whatever ledger data it needs and
returns findings. The graph (M4) fetches the data and runs the four checks in parallel;
`run_deterministic_checks` does the same sequentially for scripts and tests.

Convention: at most one finding per kind per invoice; the evidence lists every affected line.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from invoiceiq import gst
from invoiceiq.models import (
    Finding,
    FindingKind,
    Invoice,
    PostedInvoice,
    PurchaseOrder,
    RawInvoice,
    Severity,
    Vendor,
)
from invoiceiq.policy import Policy
from invoiceiq.repos.ledger import LedgerRepo

# Policy §4.2 names duplicates, blocked vendors and GST mismatches as critical.
SEVERITY: dict[FindingKind, Severity] = {
    "exact_duplicate": "critical",
    "fuzzy_duplicate": "critical",
    "blocked_vendor": "critical",
    "gst_type_mismatch": "critical",
    "price_drift": "warning",
    "unknown_vendor": "warning",
    "math_error": "warning",
    "stale_date": "warning",
    "fx_unverified": "warning",
}


def _finding(kind: FindingKind, message: str, severity: Severity | None = None, **evidence: str) -> Finding:
    return Finding(
        kind=kind,
        severity=severity or SEVERITY[kind],
        message=message,
        evidence=evidence,
        source="deterministic",
    )


# --- Strict validation --------------------------------------------------------------------------


def strict_validate(raw: RawInvoice) -> tuple[Invoice | None, list[Finding]]:
    """RawInvoice → Invoice. Rule failures become findings instead of exceptions."""
    findings: list[Finding] = []
    if math := gst.arithmetic_issues(raw):
        findings.append(_finding("math_error", "; ".join(math), issues=str(len(math))))
    if gst_type := gst.gst_type_issues(raw):
        findings.append(_finding("gst_type_mismatch", "; ".join(gst_type)))
    if findings:
        return None, findings
    return Invoice.model_validate(raw.model_dump()), []


# --- Duplicates ---------------------------------------------------------------------------------


def normalize_invoice_no(invoice_no: str) -> str:
    """'INV/26/042', 'inv-26-042' and 'INV 26 042' all become 'INV26042'."""
    return re.sub(r"[^A-Z0-9]", "", invoice_no.upper())


def content_hash(vendor_id: str, invoice_no: str, invoice_date: date, total: Decimal) -> str:
    key = f"{vendor_id}|{normalize_invoice_no(invoice_no)}|{invoice_date.isoformat()}|{gst.money(total)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def check_duplicates(
    raw: RawInvoice, vendor: Vendor | None, posted: Sequence[PostedInvoice]
) -> list[Finding]:
    """Exact: same number in the same FY. Fuzzy: same normalized number, date and total."""
    if vendor is None:
        return []
    fy = gst.financial_year(raw.invoice_date)
    for p in posted:
        if p.invoice_no == raw.invoice_no and p.fy == fy:
            return [
                _finding(
                    "exact_duplicate",
                    f"invoice {raw.invoice_no} from {vendor.name} was already posted ({p.posting_id})",
                    posting_id=p.posting_id,
                    posted_total=str(p.total),
                )
            ]
    digest = content_hash(vendor.vendor_id, raw.invoice_no, raw.invoice_date, raw.total)
    for p in posted:
        if p.content_hash == digest:
            return [
                _finding(
                    "fuzzy_duplicate",
                    f"invoice {raw.invoice_no} matches posted {p.invoice_no} ({p.posting_id}) "
                    "after normalizing the number, with the same date and total",
                    posting_id=p.posting_id,
                    posted_invoice_no=p.invoice_no,
                )
            ]
    return []


# --- PO match -----------------------------------------------------------------------------------


def check_po(raw: RawInvoice, po: PurchaseOrder | None, *, threshold_pct: Decimal) -> list[Finding]:
    """Flag unit prices more than `threshold_pct` above the PO. Exactly at the threshold is not drift."""
    if po is None:
        return []
    by_hsn = {line.hsn_sac: line for line in po.lines if line.hsn_sac}
    drifted: list[str] = []
    for i, item in enumerate(raw.line_items):
        po_line = by_hsn.get(item.hsn_sac) if item.hsn_sac else None
        if po_line is None and i < len(po.lines):
            po_line = po.lines[i]  # no HSN match: fall back to line position
        if po_line is None or po_line.unit_price <= 0:
            continue
        drift = gst.money((item.unit_price - po_line.unit_price) / po_line.unit_price * 100)
        if drift > threshold_pct:
            drifted.append(f"{item.description}: {po_line.unit_price} → {item.unit_price} (+{drift}%)")
    if not drifted:
        return []
    return [
        _finding(
            "price_drift",
            f"{len(drifted)} line(s) priced more than {threshold_pct}% above {po.po_number}",
            po_number=po.po_number,
            lines="; ".join(drifted),
        )
    ]


# --- Vendor -------------------------------------------------------------------------------------


def check_vendor(raw: RawInvoice, vendor: Vendor | None) -> list[Finding]:
    supplier = raw.supplier
    if vendor is None:
        return [
            _finding(
                "unknown_vendor",
                f"supplier {supplier.name} ({supplier.gstin or 'no GSTIN'}) is not in the vendor master",
                gstin=supplier.gstin or "",
            )
        ]
    if vendor.status == "blocked":
        return [_finding("blocked_vendor", f"vendor {vendor.name} is blocked", vendor_id=vendor.vendor_id)]
    if vendor.status == "pending_kyc":
        return [
            _finding(
                "blocked_vendor",
                f"vendor {vendor.name} has KYC pending",
                severity="warning",
                vendor_id=vendor.vendor_id,
            )
        ]
    return []


# --- Date and FX --------------------------------------------------------------------------------


def check_date_and_fx(raw: RawInvoice, *, today: date, stale_after_days: int) -> list[Finding]:
    findings: list[Finding] = []
    age = (today - raw.invoice_date).days
    if age < 0:
        findings.append(_finding("stale_date", f"invoice is dated {-age} day(s) in the future"))
    elif age > stale_after_days:
        findings.append(_finding("stale_date", f"invoice is {age} days old (limit {stale_after_days})"))
    if raw.currency != "INR":
        findings.append(
            _finding(
                "fx_unverified",
                f"invoice is in {raw.currency}; the INR amount needs an FX check",
                currency=raw.currency,
            )
        )
    return findings


# --- All together -------------------------------------------------------------------------------


async def run_deterministic_checks(
    raw: RawInvoice, *, ledger: LedgerRepo, policy: Policy, today: date
) -> list[Finding]:
    """Strict validation plus the four checks, in a fixed order."""
    _, findings = strict_validate(raw)
    vendor = await ledger.find_vendor(gstin=raw.supplier.gstin, name=raw.supplier.name)
    posted = await ledger.posted_for_vendor(vendor.vendor_id) if vendor else []
    po = await ledger.get_po(raw.po_number) if raw.po_number else None
    findings += check_duplicates(raw, vendor, posted)
    findings += check_po(raw, po, threshold_pct=policy.price_drift_pct)
    findings += check_vendor(raw, vendor)
    findings += check_date_and_fx(raw, today=today, stale_after_days=policy.stale_after_days)
    return findings
