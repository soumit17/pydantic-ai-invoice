"""Domain models (spec §6).

Two invoice models, on purpose (spec §7.1 ⚠️):
- `RawInvoice` is lenient: the numbers exactly as printed, with only format validation. It is the
  extractor's output type, so a vendor's arithmetic mistake survives extraction instead of being
  "fixed" by the model during a validation retry.
- `Invoice` is strict: the same data, plus the arithmetic and GST-type rules from `gst.py`. The
  pipeline's `strict_validate` step turns those rule failures into findings.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, cast

from pydantic import BaseModel, Field, field_validator, model_validator

from invoiceiq import gst

GSTIN = Annotated[str, Field(pattern=gst.GSTIN_PATTERN)]
Money = Annotated[Decimal, Field(max_digits=14, decimal_places=2)]
GstRate = Literal[0, 5, 12, 18, 28]
Currency = Literal["INR", "USD", "EUR"]
Severity = Literal["info", "warning", "critical"]
FindingKind = Literal[
    "exact_duplicate",
    "fuzzy_duplicate",
    "price_drift",
    "unknown_vendor",
    "blocked_vendor",
    "gst_type_mismatch",
    "math_error",
    "stale_date",
    "fx_unverified",
]
DecisionOutcome = Literal["auto_approved", "awaiting_approval", "held", "rejected"]


# --- Extraction ---------------------------------------------------------------------------------


class Party(BaseModel):
    name: str
    gstin: GSTIN | None = None
    address: str | None = None

    @property
    def state_code(self) -> str | None:
        return gst.state_code(self.gstin) if self.gstin else None


class LineItem(BaseModel):
    """One invoice line, exactly as printed."""

    description: str
    hsn_sac: str | None = Field(default=None, description="HSN (goods) or SAC (services) code")
    qty: Decimal = Field(gt=0)
    unit: str | None = None
    unit_price: Money
    taxable_value: Money
    gst_rate: GstRate | None = Field(default=None, description="GST rate in percent; null if no GST")
    cgst: Money = Decimal(0)
    sgst: Money = Decimal(0)
    igst: Money = Decimal(0)


class RawInvoice(BaseModel):
    """A vendor invoice with every number copied as printed. No arithmetic checks."""

    kind: Literal["invoice"] = "invoice"
    supplier: Party
    buyer: Party
    invoice_no: str
    invoice_date: date
    po_number: str | None = None
    currency: Currency = "INR"
    line_items: list[LineItem] = Field(min_length=1)
    subtotal: Money
    total_tax: Money
    total: Money
    confidence: float = Field(ge=0, le=1, description="Self-assessed extraction confidence")


class Invoice(RawInvoice):
    """A RawInvoice that also passes the arithmetic and GST-type rules."""

    @model_validator(mode="after")
    def _check_math_and_gst_type(self) -> Invoice:
        issues = gst.arithmetic_issues(self) + gst.gst_type_issues(self)
        if issues:
            raise ValueError("; ".join(issues))
        return self


class ExtractionFailure(BaseModel):
    """The document is not a valid invoice, or critical fields are unreadable."""

    kind: Literal["failure"] = "failure"
    reason: Literal["not_an_invoice", "unreadable", "missing_critical_fields"]
    missing_fields: list[str] = []
    detected_document_type: str | None = None


ExtractionResult = Annotated[RawInvoice | ExtractionFailure, Field(discriminator="kind")]


# --- Audit and decision -------------------------------------------------------------------------


class Finding(BaseModel):
    kind: FindingKind
    severity: Severity
    message: str
    evidence: dict[str, str] = {}
    source: Literal["deterministic", "llm"]


class Evidence(BaseModel):
    name: str
    value: str


class AuditFinding(BaseModel):
    """A finding as the auditor model writes it, converted to a `Finding` by `merge_findings`.

    Two differences from `Finding`, both on purpose (D20):
    - no `source`: the schema itself stops the model from claiming a finding is "deterministic";
    - evidence is a list of name/value pairs, not a dict. With thinking on, Pydantic AI uses
      Anthropic's strict native output, where `dict[str, str]` can't be expressed and comes back {}.
    """

    kind: FindingKind
    severity: Severity
    message: str
    evidence: list[Evidence] = []

    @field_validator("evidence", mode="before")
    @classmethod
    def _accept_legacy_dict(cls, value: object) -> object:
        # Records stored before D20 kept evidence as a dict; read them as name/value pairs.
        if isinstance(value, dict):
            legacy = cast(dict[object, object], value)
            return [{"name": str(k), "value": str(v)} for k, v in legacy.items()]
        return value

    def to_finding(self) -> Finding:
        evidence = {e.name: e.value for e in self.evidence}
        return Finding(
            kind=self.kind, severity=self.severity, message=self.message, evidence=evidence, source="llm"
        )


class AuditReport(BaseModel):
    # checks_passed gives the model somewhere to put "I checked X, it's fine". Without it, the model
    # filed passed checks as findings, some of them critical (first M3 run, D14).
    checks_passed: list[str] = Field(
        default=[], description="Checks you ran that found nothing wrong, one short line each"
    )
    findings: list[AuditFinding] = Field(
        description="Problems only, not already reported by the deterministic checks. Usually empty."
    )
    narrative: str = Field(description="2-4 sentence plain-English explanation for the approver")
    suggested_decision: Literal["approve", "needs_approval", "hold"]


class Decision(BaseModel):
    outcome: DecisionOutcome
    reasons: list[str]


class PostingReceipt(BaseModel):
    posting_id: str
    idempotency_key: str
    amount: Decimal
    posted_by: str
    posted_at: str


InvoiceStatus = Literal["processing", "failed", "rejected", "posted", "awaiting_approval", "held", "denied"]


class InvoiceRecord(BaseModel):
    """Everything the pipeline knows about one invoice: the graph's output and the stored record."""

    invoice_id: str
    file_name: str
    status: InvoiceStatus
    extraction: ExtractionResult | None = None
    findings: list[Finding] = []
    audit: AuditReport | None = None
    decision: Decision | None = None
    receipt: PostingReceipt | None = None
    timings: dict[str, float] = {}
    audit_escalation: str | None = None  # why the auditor was re-run with Thinking (M6)
    error: str | None = None


# --- Ledger rows (spec §5.1) --------------------------------------------------------------------


class Vendor(BaseModel):
    vendor_id: str
    name: str
    gstin: str | None
    state_code: str
    status: Literal["active", "blocked", "pending_kyc"]
    bank_last4: str | None = None


class POLine(BaseModel):
    line_no: int
    hsn_sac: str | None
    description: str
    qty: Decimal
    unit_price: Decimal
    gst_rate: Decimal


class PurchaseOrder(BaseModel):
    po_number: str
    vendor_id: str
    issued_on: date
    status: Literal["open", "closed", "cancelled"]
    lines: list[POLine] = []


class PostedInvoice(BaseModel):
    posting_id: str
    vendor_id: str
    invoice_no: str
    fy: str
    invoice_date: date
    total: Decimal
    currency: str
    content_hash: str
    posted_by: str
    posted_at: str
    idempotency_key: str


# --- Ground truth (spec §5.3) -------------------------------------------------------------------


class ExpectedFinding(BaseModel):
    kind: FindingKind
    severity: Severity


class GroundTruth(BaseModel):
    file_id: str
    file_name: str
    category: int
    category_name: str
    format: Literal["pdf", "png", "eml"]
    expected: ExtractionResult
    expected_findings: list[ExpectedFinding]
    expected_decision: DecisionOutcome
    notes: str = ""
