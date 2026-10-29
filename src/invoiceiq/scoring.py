"""Field-level comparison of an extraction against ground truth.

Used by the M2 live run and, from M8, by the extraction evaluators.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from invoiceiq.models import ExtractionFailure, LineItem, RawInvoice

HEADER_FIELDS = ("supplier.name", "supplier.gstin", "buyer.gstin", "invoice_no", "invoice_date",
                 "po_number", "currency", "subtotal", "total_tax", "total")  # fmt: skip
LINE_FIELDS = (
    "description",
    "hsn_sac",
    "qty",
    "unit_price",
    "taxable_value",
    "gst_rate",
    "cgst",
    "sgst",
    "igst",
)


@dataclass
class FieldScore:
    kind_ok: bool
    correct: int = 0
    total: int = 0
    wrong: list[str] = field(default_factory=lambda: [])

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else float(self.kind_ok)


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _same(a: object, b: object) -> bool:
    if isinstance(a, Decimal | int | float) and isinstance(b, Decimal | int | float):
        return abs(Decimal(str(a)) - Decimal(str(b))) < Decimal("0.01")
    if isinstance(a, str) or isinstance(b, str):
        return _norm(str(a) if a is not None else None) == _norm(str(b) if b is not None else None)
    return a == b


def _header(inv: RawInvoice, name: str) -> object:
    obj: object = inv
    for attr in name.split("."):
        obj = getattr(obj, attr)
    return obj


def _line(li: LineItem, name: str) -> object:
    return getattr(li, name)


def score(expected: RawInvoice | ExtractionFailure, got: RawInvoice | ExtractionFailure) -> FieldScore:
    """Kind must match. For invoices, every header field and every line field counts once;
    missing or extra lines count as wrong."""
    if isinstance(expected, ExtractionFailure) or isinstance(got, ExtractionFailure):
        ok = type(expected) is type(got)
        return FieldScore(kind_ok=ok, correct=int(ok), total=1, wrong=[] if ok else ["kind"])

    result = FieldScore(kind_ok=True)

    def check(label: str, want: object, have: object) -> None:
        result.total += 1
        if _same(want, have):
            result.correct += 1
        else:
            result.wrong.append(f"{label}: want {want!r}, got {have!r}")

    for name in HEADER_FIELDS:
        check(name, _header(expected, name), _header(got, name))
    for i in range(max(len(expected.line_items), len(got.line_items))):
        want = expected.line_items[i] if i < len(expected.line_items) else None
        have = got.line_items[i] if i < len(got.line_items) else None
        for name in LINE_FIELDS:
            check(
                f"line {i + 1}.{name}",
                _line(want, name) if want else "<missing>",
                _line(have, name) if have else "<missing>",
            )
    return result
