"""Generate the mock ledger and the 40-invoice corpus (spec §5). Seeded, deterministic, idempotent.

    uv run python scripts/gen_mockdata.py              # writes into data/
    uv run python scripts/gen_mockdata.py --out DIR    # anywhere else (tests use this)

Writes:
    <out>/ledger.sqlite               vendors, purchase orders, posting history
    <out>/invoices/inv-NNN.{pdf,png,eml}
    <out>/ground_truth/inv-NNN.json   expected extraction, findings and decision

Every expected finding and decision below is written by hand from the category, never computed
by the checks or the policy, so the M1 test that compares them is a real test.
"""

from __future__ import annotations

import argparse
import io
import random
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pypdfium2 as pdfium
from faker import Faker
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from invoiceiq import gst
from invoiceiq.config import PROJECT_ROOT
from invoiceiq.models import (
    DecisionOutcome,
    ExpectedFinding,
    ExtractionFailure,
    FindingKind,
    GroundTruth,
    LineItem,
    Party,
    RawInvoice,
)
from invoiceiq.pipeline.checks import SEVERITY, content_hash
from invoiceiq.repos.ledger import SCHEMA

SEED = 20261029
AS_OF = date(2026, 10, 29)
IST = timezone(timedelta(hours=5, minutes=30))

COMPANY = Party(
    name="Hooghly Precision Components Pvt Ltd",
    gstin="19ABCDE1234F1Z5",
    address="14 Strand Road, Kolkata 700001, West Bengal",
)

CITIES: dict[str, tuple[str, str]] = {  # state code -> (city, PIN prefix)
    "19": ("Kolkata", "700"),
    "27": ("Pune", "411"),
    "29": ("Bengaluru", "560"),
    "07": ("New Delhi", "110"),
    "33": ("Chennai", "600"),
    "24": ("Ahmedabad", "380"),
    "09": ("Lucknow", "226"),
    "23": ("Indore", "452"),
}

# (description, HSN/SAC, unit, min price, max price, GST rate)
ITEMS: list[tuple[str, str, str, int, int, int]] = [
    ("M8 Hex Bolt, zinc plated", "7318", "pcs", 4, 12, 18),
    ("Ball Bearing 6204-2RS", "8482", "pcs", 120, 260, 18),
    ("Corrugated Box 5-ply 18x12x12", "4819", "pcs", 35, 70, 12),
    ("Hydraulic Oil ISO VG 68 (20 L)", "2710", "can", 2800, 4200, 18),
    ("Cotton Waste, industrial", "5202", "kg", 60, 110, 5),
    ("Nitrile Safety Gloves", "4015", "pair", 45, 90, 12),
    ("Machine Maintenance Service", "998719", "visit", 3500, 9000, 18),
    ("Freight Charges", "996511", "trip", 1500, 6000, 5),
    ("Welding Electrode E6013 (5 kg)", "8311", "pack", 650, 1100, 18),
    ("Printed Label Roll", "4821", "roll", 220, 480, 12),
    ("Rice Bran, raw", "2302", "kg", 18, 30, 0),
]
HIGH_VALUE_ITEMS: list[tuple[str, str, str, int, int, int]] = [
    ("Screw Air Compressor 10 HP", "8414", "unit", 32000, 60000, 28),
    ("Induction Motor 7.5 HP", "8501", "unit", 26000, 38000, 18),
]
IMPORT_ITEMS: list[tuple[str, str, str, int, int]] = [  # USD prices, no GST
    ("Digital Caliper 0-150 mm", "9017", "pcs", 40, 90),
    ("Surface Roughness Tester", "9031", "unit", 900, 1600),
    ("Calibration Gauge Block Set", "9017", "set", 300, 700),
]


@dataclass
class VendorSpec:
    vendor_id: str
    name: str
    state: str
    status: str
    style: str  # invoice-number style
    foreign: bool = False
    in_ledger: bool = True
    gstin: str | None = None
    address: str = ""
    email: str = ""
    phone: str = ""
    account_no: str = ""
    first_no: int = 0
    next_no: int = 0


VENDORS = [
    VendorSpec("V001", "Shri Balaji Engineering Works", "19", "active", "slash"),  # Hindi/English mix
    VendorSpec("V002", "Maa Durga Packaging Udyog", "19", "active", "fy"),  # Hindi/English mix
    VendorSpec("V003", "Eastern Fasteners & Hardware", "19", "active", "dash"),
    VendorSpec("V004", "Bengal Lubricants Pvt Ltd", "19", "active", "plain"),
    VendorSpec("V005", "Howrah Industrial Services", "19", "active", "slash"),
    VendorSpec("V006", "Salt Lake Safety Supplies", "19", "active", "dash"),
    VendorSpec("V007", "Deccan Bearings Ltd", "27", "active", "dash"),
    VendorSpec("V008", "Karnataka Motors & Controls", "29", "active", "fy"),
    VendorSpec("V009", "Capital Tools Corporation", "07", "active", "plain"),
    VendorSpec("V010", "Coromandel Logistics", "33", "active", "slash"),
    VendorSpec("V011", "Sabarmati Polymers", "24", "active", "dash"),
    VendorSpec("V012", "Western Weld Consumables", "27", "blocked", "dash"),
    VendorSpec("V013", "Gomti Traders", "09", "pending_kyc", "plain"),
    VendorSpec("V014", "Lakeside Metrology Inc", "96", "active", "plain", foreign=True),
    VendorSpec("V015", "Pacific Gauge Works LLC", "96", "active", "dash", foreign=True),
    VendorSpec("VX01", "Narmada Steel Traders", "23", "active", "slash", in_ledger=False),  # unknown vendor
]
V = {v.vendor_id: v for v in VENDORS}


def invoice_number(v: VendorSpec, n: int) -> str:
    prefix = "".join(w[0] for w in v.name.split() if w[0].isalpha())[:3].upper()
    if v.style == "slash":
        return f"INV/26/{n:03d}"
    if v.style == "fy":
        return f"{prefix}/{n:04d}/26-27"
    if v.style == "dash":
        return f"{prefix}-2026-{n:04d}"
    return str(n)


@dataclass
class Plan:
    category: int
    category_name: str
    fmt: str  # pdf | png | eml
    vendor: VendorSpec
    expected_findings: list[FindingKind]
    expected_decision: DecisionOutcome
    notes: str = ""
    doc_type: str = "invoice"  # or "quotation"
    file_id: str = ""
    template: int = 0  # 0 table, 1 two-column, 2 minimal
    invoice: RawInvoice | None = None


class Generator:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.rng = random.Random(SEED)
        self.fake = Faker("en_IN")
        self.fake.seed_instance(SEED)
        self.po_seq = 0
        self.purchase_orders: list[tuple[str, str, date, list[tuple[str, str, Decimal, Decimal, int]]]] = []
        self.postings: list[tuple[str, str, str, date, Decimal, str, str]] = []

    # --- reference data ---------------------------------------------------------------------

    def make_gstin(self, state: str) -> str:
        r = self.rng
        letters = "".join(r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(5))
        digits = f"{r.randint(0, 9999):04d}"
        return (
            f"{state}{letters}{digits}{r.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}"
            f"{r.choice('123456789')}Z{r.choice('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ')}"
        )

    def init_vendors(self) -> None:
        for v in VENDORS:
            if v.foreign:
                v.address = f"{self.rng.randint(100, 9999)} Harbor Blvd, Oakland, CA 94607, USA"
                v.phone = f"+1 510 555 {self.rng.randint(1000, 9999)}"
            else:
                v.gstin = self.make_gstin(v.state)
                city, pin = CITIES[v.state]
                v.address = (
                    f"{self.fake.building_number()}, {self.fake.street_name()}, {city} "
                    f"{pin}{self.rng.randint(1, 99):03d}, {gst.STATES[v.state]}"
                )
                v.phone = f"+91 9{self.rng.randint(100000000, 999999999)}"
            v.email = f"accounts@{v.name.split()[0].lower()}.example"
            v.account_no = "".join(self.rng.choice("0123456789") for _ in range(12))
            v.next_no = v.first_no = self.rng.randint(40, 300)

    # --- line items -------------------------------------------------------------------------

    def line(
        self, desc: str, hsn: str, unit: str, qty: int, price: Decimal, rate: int | None, scheme: str
    ) -> LineItem:
        taxable = gst.money(qty * price)
        cgst = sgst = igst = Decimal(0)
        if rate is not None and scheme != "none":
            cgst, sgst, igst = gst.line_tax(taxable, rate, intra=scheme == "intra")
        return LineItem(
            description=desc,
            hsn_sac=hsn,
            qty=Decimal(qty),
            unit=unit,
            unit_price=price,
            taxable_value=taxable,
            gst_rate=rate,  # pyright: ignore[reportArgumentType]
            cgst=cgst,
            sgst=sgst,
            igst=igst,
        )

    def domestic_lines(self, n: int, scheme: str, *, high_value: bool = False) -> list[LineItem]:
        cap = Decimal(38_000) / n  # keeps the total under ₹50k even at 28% GST
        items = self.rng.sample(ITEMS, n)
        lines: list[LineItem] = []
        if high_value:
            hv = self.rng.choice(HIGH_VALUE_ITEMS)
            price = Decimal(self.rng.randint(hv[3], hv[4]) // 10 * 10)
            lines.append(self.line(hv[0], hv[1], hv[2], self.rng.randint(2, 3), price, hv[5], scheme))
            items = items[: n - 1]
        for desc, hsn, unit, lo, hi, rate in items:
            # Whole-rupee prices: +5% of a whole rupee amount is exact to the paisa, so the
            # borderline drift case lands on exactly 5.00%.
            price = Decimal(self.rng.randint(lo, hi))
            max_qty = max(1, int(cap / price)) if not high_value else max(1, int(Decimal(8000) / price))
            lines.append(self.line(desc, hsn, unit, self.rng.randint(1, max_qty), price, rate, scheme))
        return lines

    def build_invoice(
        self,
        v: VendorSpec,
        lines: list[LineItem],
        *,
        inv_date: date,
        po_number: str | None,
        currency: str = "INR",
        invoice_no: str | None = None,
    ) -> RawInvoice:
        subtotal = sum((li.taxable_value for li in lines), Decimal(0))
        tax = sum((li.cgst + li.sgst + li.igst for li in lines), Decimal(0))
        if invoice_no is None:
            invoice_no = invoice_number(v, v.next_no)
            v.next_no += 1
        return RawInvoice(
            supplier=Party(name=v.name, gstin=v.gstin, address=v.address),
            buyer=COMPANY,
            invoice_no=invoice_no,
            invoice_date=inv_date,
            po_number=po_number,
            currency=currency,  # pyright: ignore[reportArgumentType]
            line_items=lines,
            subtotal=subtotal,
            total_tax=tax,
            total=subtotal + tax,
            confidence=1.0,
        )

    def new_po(
        self, v: VendorSpec, lines: list[LineItem], issued: date, drift: dict[int, Decimal] | None = None
    ) -> str:
        """Create a PO whose prices match the invoice lines, except lines listed in `drift`,
        whose PO price is the invoice price divided back down."""
        self.po_seq += 1
        po_number = f"PO-2026-{self.po_seq:04d}"
        po_lines: list[tuple[str, str, Decimal, Decimal, int]] = []
        for i, li in enumerate(lines):
            price = li.unit_price
            if drift and i in drift:
                price = drift[i]
            po_lines.append((li.hsn_sac or "", li.description, li.qty, price, li.gst_rate or 0))
        self.purchase_orders.append((po_number, v.vendor_id, issued, po_lines))
        return po_number

    def random_date(self, lo_days: int = 3, hi_days: int = 60) -> date:
        return AS_OF - timedelta(days=self.rng.randint(lo_days, hi_days))

    # --- plans ------------------------------------------------------------------------------

    def plans(self) -> list[Plan]:
        plans: list[Plan] = []
        intra = ["V001", "V002", "V003", "V004", "V005", "V006"]
        inter = ["V007", "V008", "V009", "V010", "V011"]

        def add(plan: Plan, invoice: RawInvoice) -> None:
            plan.invoice = invoice
            plans.append(plan)

        def clean(v: VendorSpec, scheme: str, *, high_value: bool = False, use_po: bool = True) -> RawInvoice:
            lines = self.domestic_lines(
                self.rng.randint(1, 8) if not high_value else 3, scheme, high_value=high_value
            )
            d = self.random_date()
            po = self.new_po(v, lines, d - timedelta(days=self.rng.randint(5, 30))) if use_po else None
            return self.build_invoice(v, lines, inv_date=d, po_number=po)

        # 1. Clean, intra-state (10)
        for i in range(10):
            v = V[intra[i % len(intra)]]
            add(Plan(1, "clean_intra", "pdf", v, [], "auto_approved"), clean(v, "intra", use_po=i % 4 != 3))
        # 2. Clean, inter-state (6)
        for i in range(6):
            v = V[inter[i % len(inter)]]
            add(Plan(2, "clean_inter", "pdf", v, [], "auto_approved"), clean(v, "inter"))
        # 3. Clean, high value (3)
        for vid, scheme in [("V004", "intra"), ("V008", "inter"), ("V007", "inter")]:
            add(
                Plan(3, "clean_high_value", "pdf", V[vid], [], "awaiting_approval"),
                clean(V[vid], scheme, high_value=True),
            )
        # 4. Exact duplicate of a posted invoice (2)
        for vid, scheme in [("V003", "intra"), ("V009", "inter")]:
            v = V[vid]
            inv = clean(v, scheme)
            self.post(v, inv.invoice_no, inv.invoice_date, inv.total)
            add(
                Plan(
                    4,
                    "exact_duplicate",
                    "pdf",
                    v,
                    ["exact_duplicate"],
                    "held",
                    "resubmission of a posted invoice",
                ),
                inv,
            )
        # 5. Fuzzy duplicate: posted as INV-26-042, resubmitted as INV/26/042 (2)
        for vid, scheme in [("V001", "intra"), ("V010", "inter")]:
            v = V[vid]
            inv = clean(v, scheme)
            posted_no = inv.invoice_no.replace("/", "-")
            self.post(v, posted_no, inv.invoice_date, inv.total)
            add(
                Plan(
                    5,
                    "fuzzy_duplicate",
                    "pdf",
                    v,
                    ["fuzzy_duplicate"],
                    "held",
                    f"posted earlier as {posted_no}",
                ),
                inv,
            )
        # 6. Price drift vs PO (4, the last one exactly at 5%)
        for vid, scheme, pct in [
            ("V005", "intra", "8"),
            ("V007", "inter", "12"),
            ("V006", "intra", "6.5"),
            ("V002", "intra", "5"),
        ]:
            plans.append(self.drift_plan(V[vid], scheme, Decimal(pct)))
        # 7. Blocked vendor (1)
        v = V["V012"]
        add(Plan(7, "blocked_vendor", "pdf", v, ["blocked_vendor"], "held"), clean(v, "inter", use_po=False))
        # 8. Unknown vendor (1)
        v = V["VX01"]
        add(
            Plan(8, "unknown_vendor", "pdf", v, ["unknown_vendor"], "awaiting_approval"),
            clean(v, "inter", use_po=False),
        )
        # 9. GST type mismatch: IGST charged by an intra-state supplier (2)
        for vid in ["V006", "V004"]:
            v = V[vid]
            lines = self.domestic_lines(self.rng.randint(2, 4), "inter")
            d = self.random_date()
            inv = self.build_invoice(
                v, lines, inv_date=d, po_number=self.new_po(v, lines, d - timedelta(days=10))
            )
            add(
                Plan(
                    9,
                    "gst_type_mismatch",
                    "pdf",
                    v,
                    ["gst_type_mismatch"],
                    "held",
                    "IGST on an intra-state supply",
                ),
                inv,
            )
        # 10. Arithmetic errors (2): a wrong line total, then a wrong grand total
        plans.append(self.math_error_plan(V["V003"], wrong="line"))
        plans.append(self.math_error_plan(V["V011"], wrong="total"))
        # 11. Scans (3): 2 clean, 1 price drift
        for vid, scheme in [("V002", "intra"), ("V009", "inter")]:
            add(Plan(11, "scan_clean", "png", V[vid], [], "auto_approved"), clean(V[vid], scheme))
        drift_scan = self.drift_plan(V["V005"], "intra", Decimal(10))
        drift_scan.category, drift_scan.category_name, drift_scan.fmt = 11, "scan_price_drift", "png"
        plans.append(drift_scan)
        # 12. Email-body invoice (1), clean
        add(
            Plan(12, "email_clean", "eml", V["V005"], [], "auto_approved"),
            clean(V["V005"], "intra", use_po=False),
        )
        # 13. Foreign currency, USD import, no GST (2)
        for vid in ["V014", "V015"]:
            plans.append(self.import_plan(V[vid]))
        # 14. Not an invoice: a quotation (1)
        v = V["V006"]
        q = self.build_invoice(
            v,
            self.domestic_lines(3, "intra"),
            inv_date=self.random_date(),
            po_number=None,
            invoice_no=f"QT-{self.rng.randint(100, 999)}",
        )
        add(Plan(14, "not_an_invoice", "pdf", v, [], "rejected", "a quotation", doc_type="quotation"), q)

        for i, p in enumerate(plans, start=1):
            p.file_id = f"inv-{i:03d}"
            p.template = i % 3
        return plans

    def drift_plan(self, v: VendorSpec, scheme: str, pct: Decimal) -> Plan:
        lines = self.domestic_lines(self.rng.randint(2, 5), scheme)
        # Drift the most expensive line: PO price p, invoice price p × (1 + pct/100).
        idx = max(range(len(lines)), key=lambda i: lines[i].unit_price)
        po_price = lines[idx].unit_price
        old = lines[idx]
        lines[idx] = self.line(
            old.description,
            old.hsn_sac or "",
            old.unit or "",
            int(old.qty),
            gst.money(po_price * (1 + pct / 100)),
            old.gst_rate,
            scheme,
        )
        d = self.random_date()
        po = self.new_po(v, lines, d - timedelta(days=12), drift={idx: po_price})
        inv = self.build_invoice(v, lines, inv_date=d, po_number=po)
        if pct > 5:
            return Plan(
                6,
                "price_drift",
                "pdf",
                v,
                ["price_drift"],
                "awaiting_approval",
                f"+{pct}% on one line",
                invoice=inv,
            )
        return Plan(
            6,
            "price_drift_borderline",
            "pdf",
            v,
            [],
            "auto_approved",
            "exactly +5.00% on one line: not drift under the strict > 5% rule; the auditor may comment",
            invoice=inv,
        )

    def math_error_plan(self, v: VendorSpec, *, wrong: str) -> Plan:
        scheme = "intra" if v.state == "19" else "inter"
        lines = self.domestic_lines(3, scheme)
        if wrong == "line":
            # The vendor mis-multiplied one line; tax and totals are consistent with the wrong figure.
            li = lines[1]
            bad = li.taxable_value + Decimal(450)
            c, s, i = gst.line_tax(bad, li.gst_rate or 0, intra=scheme == "intra")
            lines[1] = li.model_copy(update={"taxable_value": bad, "cgst": c, "sgst": s, "igst": i})
        d = self.random_date()
        inv = self.build_invoice(v, lines, inv_date=d, po_number=self.new_po(v, lines, d - timedelta(days=9)))
        note = "line 2 taxable value ≠ qty × unit price (+₹450)"
        if wrong == "total":
            inv = inv.model_copy(update={"total": inv.total + Decimal(100)})
            note = "grand total is ₹100 more than subtotal + tax"
        return Plan(10, "math_error", "pdf", v, ["math_error"], "awaiting_approval", note, invoice=inv)

    def import_plan(self, v: VendorSpec) -> Plan:
        lines: list[LineItem] = []
        for desc, hsn, unit, lo, hi in self.rng.sample(IMPORT_ITEMS, 2):
            lines.append(
                self.line(
                    desc, hsn, unit, self.rng.randint(1, 4), Decimal(self.rng.randint(lo, hi)), None, "none"
                )
            )
        inv = self.build_invoice(v, lines, inv_date=self.random_date(), po_number=None, currency="USD")
        return Plan(
            13,
            "foreign_currency",
            "pdf",
            v,
            ["fx_unverified"],
            "awaiting_approval",
            "USD import, no GST",
            invoice=inv,
        )

    # --- ledger -----------------------------------------------------------------------------

    def post(self, v: VendorSpec, invoice_no: str, d: date, total: Decimal, currency: str = "INR") -> None:
        self.postings.append(
            (
                v.vendor_id,
                invoice_no,
                gst.financial_year(d),
                d,
                total,
                currency,
                content_hash(v.vendor_id, invoice_no, d, total),
            )
        )

    def history(self) -> None:
        """2-3 older postings per active vendor, numbered below its current sequence."""
        for v in VENDORS:
            if not v.in_ledger or v.status != "active":
                continue
            for k in range(self.rng.randint(2, 3)):
                n = v.first_no - 1 - 3 * k  # always below every number on the new invoices
                d = AS_OF - timedelta(days=self.rng.randint(70, 260))
                total = gst.money(Decimal(self.rng.randint(400_000, 4_500_000)) / 100)
                self.post(v, invoice_number(v, n), d, total, "USD" if v.foreign else "INR")

    def write_ledger(self) -> None:
        path = self.out / "ledger.sqlite"
        path.unlink(missing_ok=True)
        con = sqlite3.connect(path)
        con.executescript(SCHEMA)
        for v in VENDORS:
            if v.in_ledger:
                con.execute(
                    "INSERT INTO vendors VALUES (?,?,?,?,?,?)",
                    (v.vendor_id, v.name, v.gstin, v.state, v.status, v.account_no[-4:]),
                )
        for po_number, vid, issued, lines in self.purchase_orders:
            con.execute(
                "INSERT INTO purchase_orders VALUES (?,?,?,?)", (po_number, vid, issued.isoformat(), "open")
            )
            for n, (hsn, desc, qty, price, rate) in enumerate(lines, start=1):
                con.execute(
                    "INSERT INTO po_lines VALUES (?,?,?,?,?,?,?)",
                    (po_number, n, hsn, desc, str(qty), str(price), rate),
                )
        for n, (vid, inv_no, fy, d, total, cur, digest) in enumerate(
            sorted(self.postings, key=lambda p: (p[3], p[0], p[1])), start=1
        ):
            posted_at = datetime.combine(d + timedelta(days=3), datetime.min.time()).isoformat()
            con.execute(
                "INSERT INTO posted_invoices VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"P-{n:05d}",
                    vid,
                    inv_no,
                    fy,
                    d.isoformat(),
                    str(total),
                    cur,
                    digest,
                    "seed",
                    posted_at,
                    f"seed-{digest}",
                ),
            )
        con.commit()
        con.close()

    # --- rendering --------------------------------------------------------------------------

    def render(self, plan: Plan) -> None:
        inv = plan.invoice
        assert inv is not None
        inv_dir = self.out / "invoices"
        if plan.fmt == "eml":
            (inv_dir / f"{plan.file_id}.eml").write_bytes(render_eml(plan, inv))
            return
        pdf = render_pdf(plan, inv)
        if plan.fmt == "pdf":
            (inv_dir / f"{plan.file_id}.pdf").write_bytes(pdf)
        else:
            (inv_dir / f"{plan.file_id}.png").write_bytes(self.scan(pdf))

    def scan(self, pdf: bytes) -> bytes:
        """Render to ≤1600 px on the long edge, rotate ±3°, add noise: a cheap phone scan."""
        doc = pdfium.PdfDocument(pdf)
        page = doc[0]
        scale = 1600 / max(page.get_size())
        img = page.render(scale=scale).to_pil().convert("L")
        page.close()
        doc.close()
        img = img.rotate(
            self.rng.uniform(-3, 3), resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255
        )
        noise = Image.frombytes("L", img.size, self.rng.randbytes(img.size[0] * img.size[1]))
        img = Image.blend(img, noise, 0.10)
        if max(img.size) > 1600:
            img.thumbnail((1600, 1600))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # --- main -------------------------------------------------------------------------------

    def run(self) -> list[Plan]:
        for sub in ("invoices", "ground_truth"):
            shutil.rmtree(self.out / sub, ignore_errors=True)
            (self.out / sub).mkdir(parents=True)
        self.init_vendors()
        plans = self.plans()
        self.history()
        self.write_ledger()
        for plan in plans:
            self.render(plan)
            write_ground_truth(self.out, plan)
        return plans


# --- formatting -----------------------------------------------------------------------------


def fmt_inr(x: Decimal) -> str:
    """Indian digit grouping: 1234567.5 → 12,34,567.50."""
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join([head, *groups, tail] if head else [*groups, tail])
    return f"{whole}.{frac}"


def fmt_amount(x: Decimal, currency: str) -> str:
    return fmt_inr(x) if currency == "INR" else f"{x:,.2f}"


def fmt_qty(q: Decimal) -> str:
    return str(int(q)) if q == q.to_integral_value() else str(q)


def tax_columns(inv: RawInvoice) -> list[str]:
    if inv.currency != "INR":
        return []
    if any(li.igst > 0 for li in inv.line_items):
        return ["IGST"]
    return ["CGST", "SGST"]


def tax_value(li: LineItem, col: str) -> Decimal:
    return {"CGST": li.cgst, "SGST": li.sgst, "IGST": li.igst}[col]


def doc_title(plan: Plan) -> str:
    if plan.doc_type == "quotation":
        return "QUOTATION"
    return "COMMERCIAL INVOICE" if plan.invoice and plan.invoice.currency != "INR" else "TAX INVOICE"


def doc_number_label(plan: Plan) -> str:
    return "Quotation No" if plan.doc_type == "quotation" else "Invoice No"


# --- PDF templates --------------------------------------------------------------------------


def render_pdf(plan: Plan, inv: RawInvoice) -> bytes:
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=A4, invariant=1)  # invariant=1: no timestamps/IDs, so output is reproducible
    c.setTitle(f"{doc_title(plan)} {inv.invoice_no}")
    templates: list[Callable[[Canvas, Plan, RawInvoice], None]] = [
        template_table,
        template_two_column,
        template_minimal,
    ]
    templates[plan.template](c, plan, inv)
    c.showPage()
    c.save()
    return buf.getvalue()


def footer(c: Canvas, plan: Plan, y: float) -> None:
    v = plan.vendor
    c.setFont("Helvetica", 8)
    if plan.doc_type == "quotation":
        c.drawString(40, y, "This is a quotation, not a demand for payment. Prices valid for 30 days.")
        y -= 12
    bank = "Chase Bank, SWIFT CHASUS33" if v.foreign else "State Bank of India, IFSC SBIN0001234"
    c.drawString(40, y, f"Bank: {bank}   A/c No: {v.account_no}")
    c.drawString(40, y - 12, f"Phone: {v.phone}   Email: {v.email}")
    c.drawString(
        40,
        y - 24,
        "Subject to Kolkata jurisdiction. E. & O.E." if not v.foreign else "Payment terms: Net 45.",
    )


def template_table(c: Canvas, plan: Plan, inv: RawInvoice) -> None:
    """Layout 1: boxed header, ruled item table, totals block on the right."""
    w, h = A4
    c.setFillGray(0.85)
    c.rect(30, h - 70, w - 60, 40, fill=1, stroke=0)
    c.setFillGray(0)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(w / 2, h - 57, doc_title(plan))

    y = h - 95
    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, inv.supplier.name)
    c.setFont("Helvetica", 9)
    c.drawString(40, y - 13, inv.supplier.address or "")
    if inv.supplier.gstin:
        c.drawString(40, y - 26, f"GSTIN: {inv.supplier.gstin}")
    c.drawRightString(w - 40, y, f"{doc_number_label(plan)}: {inv.invoice_no}")
    c.drawRightString(w - 40, y - 13, f"Date: {inv.invoice_date:%d/%m/%Y}")
    if inv.po_number:
        c.drawRightString(w - 40, y - 26, f"PO Ref: {inv.po_number}")

    y -= 60
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Bill To:")
    c.setFont("Helvetica", 9)
    c.drawString(40, y - 13, inv.buyer.name)
    c.drawString(40, y - 26, inv.buyer.address or "")
    c.drawString(40, y - 39, f"GSTIN: {inv.buyer.gstin}")

    taxes = tax_columns(inv)
    cols = ["#", "Description", "HSN/SAC", "Qty", "Unit", "Rate", "Taxable"] + (
        ["GST%"] + taxes if taxes else []
    )
    xs = [40, 58, 210, 262, 292, 325, 385] + (
        [445, 475, 520] if len(taxes) == 2 else [445, 480] if taxes else []
    )
    y -= 65
    c.setFont("Helvetica-Bold", 8)
    for x, col in zip(xs, cols, strict=True):
        c.drawString(x, y, col)
    c.line(36, y - 4, w - 36, y - 4)
    c.setFont("Helvetica", 8)
    for n, li in enumerate(inv.line_items, start=1):
        y -= 16
        row = [
            str(n),
            li.description,
            li.hsn_sac or "",
            fmt_qty(li.qty),
            li.unit or "",
            fmt_amount(li.unit_price, inv.currency),
            fmt_amount(li.taxable_value, inv.currency),
        ]
        if taxes:
            row += [f"{li.gst_rate}%"] + [fmt_amount(tax_value(li, t), inv.currency) for t in taxes]
        for x, cell in zip(xs, row, strict=True):
            c.drawString(x, y, cell)
        c.line(36, y - 5, w - 36, y - 5)

    y -= 30
    rows = [("Taxable Value", inv.subtotal)]
    for t in taxes:
        rows.append((t, sum((tax_value(li, t) for li in inv.line_items), Decimal(0))))
    rows.append((f"Grand Total ({inv.currency})", inv.total))
    for label, amount in rows:
        c.setFont("Helvetica-Bold" if label.startswith("Grand") else "Helvetica", 9)
        c.drawString(360, y, label)
        c.drawRightString(w - 40, y, fmt_amount(amount, inv.currency))
        y -= 14
    footer(c, plan, 90)


def template_two_column(c: Canvas, plan: Plan, inv: RawInvoice) -> None:
    """Layout 2: supplier and buyer side by side, unruled items, tax shown per line as one figure."""
    w, h = A4
    c.setFont("Helvetica-Bold", 13)
    c.drawString(40, h - 50, doc_title(plan))
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 40, h - 50, f"{doc_number_label(plan)}  {inv.invoice_no}")
    c.drawRightString(w - 40, h - 63, f"Dated  {inv.invoice_date:%d/%m/%Y}")

    def party(x: float, title: str, p: Party) -> None:
        y = h - 100
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x, y, title)
        c.setFont("Helvetica", 9)
        c.drawString(x, y - 13, p.name)
        for k, part in enumerate((p.address or "").split(", ")):
            c.drawString(x, y - 26 - 11 * k, part)
        if p.gstin:
            c.drawString(x, y - 80, f"GSTIN/UIN: {p.gstin}")

    party(40, "Supplier", inv.supplier)
    party(310, "Buyer (Bill to)", inv.buyer)

    y = h - 205
    c.setFont("Helvetica", 9)
    if inv.po_number:
        c.drawString(40, y, f"Buyer's Order No.: {inv.po_number}")
    y -= 25
    taxes = tax_columns(inv)
    head = ["Sl", "Description of Goods/Services", "HSN/SAC", "Quantity", "Rate", "Amount"]
    xs = [40, 60, 250, 305, 365, 430]
    if taxes:
        head += ["GST", "+".join(taxes)]
        xs += [490, 520]
    c.setFont("Helvetica-Bold", 8)
    for x, col in zip(xs, head, strict=True):
        c.drawString(x, y, col)
    c.setFont("Helvetica", 8)
    for n, li in enumerate(inv.line_items, start=1):
        y -= 15
        row = [
            str(n),
            li.description,
            li.hsn_sac or "",
            f"{fmt_qty(li.qty)} {li.unit or ''}",
            fmt_amount(li.unit_price, inv.currency),
            fmt_amount(li.taxable_value, inv.currency),
        ]
        if taxes:
            row += [f"{li.gst_rate}%", " + ".join(fmt_amount(tax_value(li, t), inv.currency) for t in taxes)]
        for x, cell in zip(xs, row, strict=True):
            c.drawString(x, y, cell)

    y -= 35
    c.line(40, y + 18, w - 40, y + 18)
    c.setFont("Helvetica", 9)
    c.drawString(40, y, f"Taxable amount: {fmt_amount(inv.subtotal, inv.currency)}")
    if taxes:
        parts = [
            f"{t} {fmt_amount(sum((tax_value(li, t) for li in inv.line_items), Decimal(0)), inv.currency)}"
            for t in taxes
        ]
        c.drawString(
            40,
            y - 13,
            "Tax: " + ",  ".join(parts) + f"   (total tax {fmt_amount(inv.total_tax, inv.currency)})",
        )
    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y - 34, f"Amount Payable: {inv.currency} {fmt_amount(inv.total, inv.currency)}")
    footer(c, plan, 80)


def template_minimal(c: Canvas, plan: Plan, inv: RawInvoice) -> None:
    """Layout 3: plain text, one item per line, dates written as 29-Oct-2026."""
    w, h = A4
    c.setFont("Helvetica", 10)
    lines = [
        inv.supplier.name.upper(),
        inv.supplier.address or "",
        f"GSTIN {inv.supplier.gstin}" if inv.supplier.gstin else "",
        "",
        f"{doc_title(plan).title()} {inv.invoice_no}    {inv.invoice_date:%d-%b-%Y}",
        f"PO: {inv.po_number}" if inv.po_number else "",
        f"To: {inv.buyer.name}, {inv.buyer.address}",
        f"GSTIN {inv.buyer.gstin}",
        "",
    ]
    for n, li in enumerate(inv.line_items, start=1):
        text = (
            f"{n}. {li.description} (HSN {li.hsn_sac}) - {fmt_qty(li.qty)} {li.unit} x "
            f"{fmt_amount(li.unit_price, inv.currency)} = {fmt_amount(li.taxable_value, inv.currency)}"
        )
        lines.append(text)
        if inv.currency == "INR" and li.gst_rate is not None:
            taxes = ", ".join(f"{t} {fmt_amount(tax_value(li, t), inv.currency)}" for t in tax_columns(inv))
            lines.append(f"     GST {li.gst_rate}%: {taxes}")
    lines += [
        "",
        f"Sub-total {fmt_amount(inv.subtotal, inv.currency)}",
        f"Tax {fmt_amount(inv.total_tax, inv.currency)}"
        if inv.currency == "INR"
        else "Tax: not applicable (export)",
        f"TOTAL {inv.currency} {fmt_amount(inv.total, inv.currency)}",
    ]
    y = h - 60
    for text in lines:
        c.drawString(50, y, text)
        y -= 15
    footer(c, plan, 80)


# --- email ----------------------------------------------------------------------------------


def render_eml(plan: Plan, inv: RawInvoice) -> bytes:
    v = plan.vendor
    body = [
        "Dear Accounts Team,",
        "",
        "Please find our invoice details below for services rendered. Kindly process payment.",
        "",
        f"Invoice No: {inv.invoice_no}",
        f"Invoice Date: {inv.invoice_date:%d/%m/%Y}",
        f"Supplier: {inv.supplier.name}, {inv.supplier.address}",
        f"Supplier GSTIN: {inv.supplier.gstin}",
        f"Bill To: {inv.buyer.name}, {inv.buyer.address}",
        f"Buyer GSTIN: {inv.buyer.gstin}",
        "",
    ]
    for n, li in enumerate(inv.line_items, start=1):
        taxes = ", ".join(f"{t} Rs.{fmt_inr(tax_value(li, t))}" for t in tax_columns(inv))
        body.append(
            f"{n}) {li.description}, SAC/HSN {li.hsn_sac}, {fmt_qty(li.qty)} {li.unit}"
            f" @ Rs.{fmt_inr(li.unit_price)} = Rs.{fmt_inr(li.taxable_value)}; GST {li.gst_rate}% ({taxes})"
        )
    body += [
        "",
        f"Taxable value: Rs.{fmt_inr(inv.subtotal)}",
        f"Total GST: Rs.{fmt_inr(inv.total_tax)}",
        f"Invoice total: Rs.{fmt_inr(inv.total)}",
        "",
        f"Bank: State Bank of India, A/c No: {v.account_no}, IFSC SBIN0001234",
        "",
        "Regards,",
        "Accounts, " + v.name,
        v.phone,
    ]
    msg = EmailMessage()
    msg["From"] = f"{v.name} <{v.email}>"
    msg["To"] = "Accounts Payable <ap@hooghlyprecision.example>"
    msg["Subject"] = f"Invoice {inv.invoice_no} from {v.name}"
    msg["Date"] = format_datetime(
        datetime.combine(inv.invoice_date, datetime.min.time()).replace(hour=11, tzinfo=IST)
    )
    msg["Message-ID"] = f"<{plan.file_id}@{v.email.split('@')[1]}>"
    msg.set_content("\n".join(body))
    return msg.as_bytes()


# --- ground truth ---------------------------------------------------------------------------


def write_ground_truth(out: Path, plan: Plan) -> None:
    assert plan.invoice is not None
    expected = (
        ExtractionFailure(reason="not_an_invoice", detected_document_type="quotation")
        if plan.doc_type == "quotation"
        else plan.invoice
    )
    gt = GroundTruth(
        file_id=plan.file_id,
        file_name=f"{plan.file_id}.{plan.fmt}",
        category=plan.category,
        category_name=plan.category_name,
        format=plan.fmt,  # pyright: ignore[reportArgumentType]
        expected=expected,
        expected_findings=[ExpectedFinding(kind=k, severity=SEVERITY[k]) for k in plan.expected_findings],
        expected_decision=plan.expected_decision,
        notes=plan.notes,
    )
    (out / "ground_truth" / f"{plan.file_id}.json").write_text(
        gt.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "data")
    args = parser.parse_args()
    plans = Generator(args.out).run()

    counts: dict[tuple[int, str], int] = {}
    for p in plans:
        counts[(p.category, p.category_name)] = counts.get((p.category, p.category_name), 0) + 1
    print(f"wrote {len(plans)} documents to {args.out}")
    for (cat, name), n in sorted(counts.items()):
        print(f"  {cat:>2} {name:<24} {n}")


if __name__ == "__main__":
    main()
