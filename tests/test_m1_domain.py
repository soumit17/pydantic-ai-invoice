from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from invoiceiq import gst
from invoiceiq.models import (
    ExtractionFailure,
    Finding,
    Invoice,
    LineItem,
    Party,
    POLine,
    PostedInvoice,
    PurchaseOrder,
    RawInvoice,
    Vendor,
)
from invoiceiq.pipeline import checks
from invoiceiq.policy import Policy, to_inr

US = "19ABCDE1234F1Z5"  # West Bengal
WB_VENDOR = "19PQRST6789K1Z2"
MH_VENDOR = "27PQRST6789K1Z2"


def line(
    price: str = "100.00", qty: int = 10, rate: int = 18, *, intra: bool = True, hsn: str = "7318"
) -> LineItem:
    taxable = gst.money(Decimal(price) * qty)
    c, s, i = gst.line_tax(taxable, rate, intra=intra)
    return LineItem(
        description="Bolt",
        hsn_sac=hsn,
        qty=Decimal(qty),
        unit_price=Decimal(price),
        taxable_value=taxable,
        gst_rate=rate,
        cgst=c,
        sgst=s,
        igst=i,  # pyright: ignore[reportArgumentType]
    )


def invoice(*lines: LineItem, supplier_gstin: str | None = WB_VENDOR, **kw: object) -> RawInvoice:
    items = list(lines) or [line()]
    subtotal = sum((li.taxable_value for li in items), Decimal(0))
    tax = sum((li.cgst + li.sgst + li.igst for li in items), Decimal(0))
    data: dict[str, object] = {
        "supplier": Party(name="Acme Bolts", gstin=supplier_gstin),
        "buyer": Party(name="Us", gstin=US),
        "invoice_no": "INV/26/042",
        "invoice_date": date(2026, 10, 1),
        "line_items": items,
        "subtotal": subtotal,
        "total_tax": tax,
        "total": subtotal + tax,
        "confidence": 1.0,
    }
    data.update(kw)
    return RawInvoice.model_validate(data)


def vendor(status: str = "active") -> Vendor:
    return Vendor(vendor_id="V1", name="Acme Bolts", gstin=WB_VENDOR, state_code="19", status=status)  # pyright: ignore[reportArgumentType]


# --- GST rules ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (US, True),
        ("27AAPFU0939F1ZV", True),
        ("19ABCDE1234F0Z5", False),
        ("19abcde1234F1Z5", False),
        ("19ABCDE1234F1Z", False),
    ],
)
def test_gstin_format(value: str, ok: bool) -> None:
    assert gst.is_valid_gstin(value) is ok


def test_party_rejects_malformed_gstin() -> None:
    with pytest.raises(ValidationError):
        Party(name="x", gstin="NOT-A-GSTIN")


@pytest.mark.parametrize(
    ("d", "fy"),
    [(date(2026, 4, 1), "2026-27"), (date(2027, 3, 31), "2026-27"), (date(2026, 3, 31), "2025-26")],
)
def test_financial_year(d: date, fy: str) -> None:
    assert gst.financial_year(d) == fy


def test_intra_state_splits_tax_equally() -> None:
    assert gst.line_tax(Decimal("1000.00"), 18, intra=True) == (
        Decimal("90.00"),
        Decimal("90.00"),
        Decimal(0),
    )
    assert gst.line_tax(Decimal("1000.00"), 18, intra=False) == (Decimal(0), Decimal(0), Decimal("180.00"))


def test_raw_invoice_accepts_vendor_arithmetic_errors_but_invoice_rejects_them() -> None:
    bad = line().model_copy(update={"taxable_value": Decimal("1450.00")})
    raw = invoice(bad)  # lenient: the vendor's mistake survives extraction
    assert gst.arithmetic_issues(raw)
    with pytest.raises(ValidationError, match="qty × unit price"):
        Invoice.model_validate(raw.model_dump())


def test_igst_on_intra_state_is_a_gst_type_error() -> None:
    raw = invoice(line(intra=False))
    assert gst.gst_type_issues(raw) == ["IGST charged on an intra-state supply (both parties in state 19)"]
    with pytest.raises(ValidationError, match="IGST"):
        Invoice.model_validate(raw.model_dump())


def test_cgst_on_inter_state_is_a_gst_type_error() -> None:
    assert gst.gst_type_issues(invoice(line(intra=True), supplier_gstin=MH_VENDOR))


def test_header_totals_within_one_rupee_pass() -> None:
    raw = invoice()
    assert not gst.arithmetic_issues(raw.model_copy(update={"total": raw.total + Decimal("0.99")}))
    assert gst.arithmetic_issues(raw.model_copy(update={"total": raw.total + Decimal("1.01")}))


# --- Policy (spec §4.2, all five rows) ----------------------------------------------------------


def f(kind: str, severity: str) -> Finding:
    return Finding(kind=kind, severity=severity, message="m", source="deterministic")  # pyright: ignore[reportArgumentType]


POLICY = Policy()


def test_policy_clean_low_value_auto_approves() -> None:
    assert POLICY.decide([], total_inr=Decimal(50_000)).outcome == "auto_approved"  # ≤ is inclusive


def test_policy_clean_high_value_needs_approval() -> None:
    assert POLICY.decide([], total_inr=Decimal("50000.01")).outcome == "awaiting_approval"


def test_policy_warning_needs_approval() -> None:
    assert POLICY.decide([f("price_drift", "warning")], total_inr=Decimal(10)).outcome == "awaiting_approval"


def test_policy_critical_holds_even_with_warnings() -> None:
    findings = [f("price_drift", "warning"), f("exact_duplicate", "critical")]
    decision = POLICY.decide(findings, total_inr=Decimal(10))
    assert decision.outcome == "held"
    assert decision.reasons[0].startswith("critical: exact_duplicate")


def test_policy_extraction_failure_rejects() -> None:
    failure = ExtractionFailure(reason="not_an_invoice", detected_document_type="quotation")
    assert POLICY.decide([], failure=failure).outcome == "rejected"


def test_policy_info_findings_do_not_block() -> None:
    assert POLICY.decide([f("stale_date", "info")], total_inr=Decimal(10)).outcome == "auto_approved"


def test_to_inr() -> None:
    assert to_inr(Decimal(100), "USD") == Decimal("8400.00")


# --- Deterministic checks -----------------------------------------------------------------------


def posted(invoice_no: str, *, total: Decimal, d: date = date(2026, 10, 1)) -> PostedInvoice:
    return PostedInvoice(
        posting_id="P1",
        vendor_id="V1",
        invoice_no=invoice_no,
        fy=gst.financial_year(d),
        invoice_date=d,
        total=total,
        currency="INR",
        content_hash=checks.content_hash("V1", invoice_no, d, total),
        posted_by="t",
        posted_at="t",
        idempotency_key="k",
    )


def test_exact_duplicate() -> None:
    raw = invoice()
    [finding] = checks.check_duplicates(raw, vendor(), [posted("INV/26/042", total=raw.total)])
    assert (finding.kind, finding.severity) == ("exact_duplicate", "critical")


def test_fuzzy_duplicate_with_reformatted_number() -> None:
    raw = invoice()
    [finding] = checks.check_duplicates(raw, vendor(), [posted("INV-26-042", total=raw.total)])
    assert finding.kind == "fuzzy_duplicate"


def test_same_number_different_total_is_not_a_fuzzy_duplicate() -> None:
    raw = invoice()
    assert checks.check_duplicates(raw, vendor(), [posted("INV-26-042", total=raw.total + 5)]) == []


def po(price: str) -> PurchaseOrder:
    return PurchaseOrder(
        po_number="PO-2026-0001",
        vendor_id="V1",
        issued_on=date(2026, 9, 1),
        status="open",
        lines=[
            POLine(
                line_no=1,
                hsn_sac="7318",
                description="Bolt",
                qty=Decimal(10),
                unit_price=Decimal(price),
                gst_rate=Decimal(18),
            )
        ],
    )


@pytest.mark.parametrize(
    ("invoice_price", "flagged"),
    [("105.01", True), ("105.00", False), ("95.00", False), ("100.00", False)],
)
def test_price_drift_threshold_is_strictly_greater_than_5pct(invoice_price: str, flagged: bool) -> None:
    findings = checks.check_po(invoice(line(invoice_price)), po("100.00"), threshold_pct=Decimal(5))
    assert bool(findings) is flagged


def test_vendor_checks() -> None:
    raw = invoice()
    assert checks.check_vendor(raw, vendor()) == []
    assert checks.check_vendor(raw, None)[0].kind == "unknown_vendor"
    assert checks.check_vendor(raw, vendor("blocked"))[0].severity == "critical"
    pending = checks.check_vendor(raw, vendor("pending_kyc"))[0]
    assert (pending.kind, pending.severity) == ("blocked_vendor", "warning")


@pytest.mark.parametrize(
    ("d", "kinds"),
    [(date(2026, 10, 1), []), (date(2026, 11, 2), ["stale_date"]), (date(2026, 3, 1), ["stale_date"])],
)
def test_date_checks(d: date, kinds: list[str]) -> None:
    found = checks.check_date_and_fx(invoice(invoice_date=d), today=date(2026, 10, 29), stale_after_days=180)
    assert [x.kind for x in found] == kinds


def test_foreign_currency_needs_fx_check() -> None:
    raw = invoice(currency="USD", supplier_gstin=None)
    found = checks.check_date_and_fx(raw, today=date(2026, 10, 29), stale_after_days=180)
    assert [x.kind for x in found] == ["fx_unverified"]
    assert gst.gst_type_issues(raw) == []  # no GST on imports


def test_strict_validate_returns_invoice_when_clean() -> None:
    inv, findings = checks.strict_validate(invoice())
    assert isinstance(inv, Invoice) and findings == []
