"""Read access to the company ledger (`data/ledger.sqlite`, spec §5.1).

Posting (with the idempotency key) arrives in M4.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from invoiceiq import gst
from invoiceiq.config import Settings
from invoiceiq.gst import money
from invoiceiq.models import POLine, PostedInvoice, PurchaseOrder, Vendor

SCHEMA = """
CREATE TABLE vendors (
  vendor_id   TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  gstin       TEXT UNIQUE,
  state_code  TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('active','blocked','pending_kyc')),
  bank_last4  TEXT
);
CREATE TABLE purchase_orders (
  po_number   TEXT PRIMARY KEY,
  vendor_id   TEXT REFERENCES vendors,
  issued_on   DATE,
  status      TEXT CHECK (status IN ('open','closed','cancelled'))
);
CREATE TABLE po_lines (
  po_number   TEXT REFERENCES purchase_orders,
  line_no     INTEGER,
  hsn_sac     TEXT,
  description TEXT,
  qty         NUMERIC,
  unit_price  NUMERIC,
  gst_rate    NUMERIC,
  PRIMARY KEY (po_number, line_no)
);
CREATE TABLE posted_invoices (
  posting_id      TEXT PRIMARY KEY,
  vendor_id       TEXT REFERENCES vendors,
  invoice_no      TEXT,
  fy              TEXT,
  invoice_date    DATE,
  total           NUMERIC,
  currency        TEXT,
  content_hash    TEXT,
  posted_by       TEXT,
  posted_at       TIMESTAMP,
  idempotency_key TEXT UNIQUE
);
CREATE INDEX posted_by_vendor ON posted_invoices (vendor_id);
CREATE INDEX posted_by_hash ON posted_invoices (content_hash);
"""


def _dec(value: Any) -> Decimal:
    # SQLite NUMERIC stores decimals as REAL; str() first avoids binary-float noise.
    return Decimal(str(value))


def _vendor(row: sqlite3.Row) -> Vendor:
    return Vendor(
        vendor_id=row["vendor_id"],
        name=row["name"],
        gstin=row["gstin"],
        state_code=row["state_code"],
        status=row["status"],
        bank_last4=row["bank_last4"],
    )


def _posted(row: sqlite3.Row) -> PostedInvoice:
    return PostedInvoice(
        posting_id=row["posting_id"],
        vendor_id=row["vendor_id"],
        invoice_no=row["invoice_no"],
        fy=row["fy"],
        invoice_date=date.fromisoformat(row["invoice_date"]),
        total=money(_dec(row["total"])),
        currency=row["currency"],
        content_hash=row["content_hash"],
        posted_by=row["posted_by"],
        posted_at=row["posted_at"],
        idempotency_key=row["idempotency_key"],
    )


class LedgerRepo:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def _fetch(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        # One short-lived connection per query: simple, and safe across asyncio tasks.
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            async with db.execute(sql, params) as cur:
                return list(await cur.fetchall())

    async def get_vendor(self, vendor_id: str) -> Vendor | None:
        rows = await self._fetch("SELECT * FROM vendors WHERE vendor_id = ?", (vendor_id,))
        return _vendor(rows[0]) if rows else None

    async def find_vendor(self, *, gstin: str | None, name: str) -> Vendor | None:
        """By GSTIN when there is one; foreign vendors have none, so fall back to an exact name match."""
        if gstin:
            rows = await self._fetch("SELECT * FROM vendors WHERE gstin = ?", (gstin,))
        else:
            rows = await self._fetch(
                "SELECT * FROM vendors WHERE gstin IS NULL AND lower(name) = lower(?)", (name.strip(),)
            )
        return _vendor(rows[0]) if rows else None

    async def get_po(self, po_number: str) -> PurchaseOrder | None:
        rows = await self._fetch("SELECT * FROM purchase_orders WHERE po_number = ?", (po_number,))
        if not rows:
            return None
        line_rows = await self._fetch(
            "SELECT * FROM po_lines WHERE po_number = ? ORDER BY line_no", (po_number,)
        )
        lines = [
            POLine(
                line_no=r["line_no"],
                hsn_sac=r["hsn_sac"],
                description=r["description"],
                qty=_dec(r["qty"]),
                unit_price=money(_dec(r["unit_price"])),
                gst_rate=_dec(r["gst_rate"]),
            )
            for r in line_rows
        ]
        po = rows[0]
        return PurchaseOrder(
            po_number=po["po_number"],
            vendor_id=po["vendor_id"],
            issued_on=date.fromisoformat(po["issued_on"]),
            status=po["status"],
            lines=lines,
        )

    async def posted_for_vendor(self, vendor_id: str) -> list[PostedInvoice]:
        rows = await self._fetch(
            "SELECT * FROM posted_invoices WHERE vendor_id = ? ORDER BY invoice_date, posting_id",
            (vendor_id,),
        )
        return [_posted(r) for r in rows]

    async def vendor_history(self, vendor_id: str, *, months: int, as_of: date) -> list[PostedInvoice]:
        since = as_of - timedelta(days=30 * months)
        rows = await self._fetch(
            "SELECT * FROM posted_invoices WHERE vendor_id = ? AND invoice_date >= ? "
            "ORDER BY invoice_date, posting_id",
            (vendor_id, since.isoformat()),
        )
        return [_posted(r) for r in rows]

    async def search_posted(self, query: str, *, limit: int = 10) -> list[PostedInvoice]:
        """Posted invoices whose number contains `query`, ignoring punctuation and case."""
        needle = re.sub(r"[^A-Z0-9]", "", query.upper())
        rows = await self._fetch("SELECT * FROM posted_invoices ORDER BY invoice_date DESC, posting_id")
        hits = [r for r in rows if needle in re.sub(r"[^A-Z0-9]", "", str(r["invoice_no"]).upper())]
        return [_posted(r) for r in hits[:limit]]

    async def search_vendors(self, query: str, *, limit: int = 10) -> list[Vendor]:
        """Vendors whose name contains `query` (case-insensitive)."""
        rows = await self._fetch(
            "SELECT * FROM vendors WHERE lower(name) LIKE ? ORDER BY name LIMIT ?",
            (f"%{query.strip().lower()}%", limit),
        )
        return [_vendor(r) for r in rows]

    async def postings_between(self, vendor_id: str, start: date, end: date) -> list[PostedInvoice]:
        """Postings for a vendor with an invoice date in [start, end], oldest first."""
        rows = await self._fetch(
            "SELECT * FROM posted_invoices WHERE vendor_id = ? AND invoice_date BETWEEN ? AND ? "
            "ORDER BY invoice_date, posting_id",
            (vendor_id, start.isoformat(), end.isoformat()),
        )
        return [_posted(r) for r in rows]

    async def get_posting(self, idempotency_key: str) -> PostedInvoice | None:
        rows = await self._fetch(
            "SELECT * FROM posted_invoices WHERE idempotency_key = ?", (idempotency_key,)
        )
        return _posted(rows[0]) if rows else None

    async def post_invoice(
        self,
        *,
        vendor_id: str | None,
        invoice_no: str,
        invoice_date: date,
        amount: Decimal,
        currency: str,
        content_hash: str,
        posted_by: str,
        idempotency_key: str,
    ) -> tuple[PostedInvoice, bool]:
        """Post once per idempotency key. Returns (posting, created); a repeat returns the first posting.

        ON CONFLICT DO NOTHING makes the database the guard: whatever retries, restarts or double
        clicks happen upstream, one key can only ever produce one row.
        """
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO posted_invoices VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    f"P-{idempotency_key}",
                    vendor_id,
                    invoice_no,
                    gst.financial_year(invoice_date),
                    invoice_date.isoformat(),
                    str(amount),
                    currency,
                    content_hash,
                    posted_by,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    idempotency_key,
                ),
            )
            created = cur.rowcount == 1
            await db.commit()
        posting = await self.get_posting(idempotency_key)
        assert posting is not None
        return posting, created


def open_ledger(settings: Settings) -> LedgerRepo:
    """The working ledger in data/runtime/, copied from the committed seed on first use."""
    if not settings.ledger_path.exists():
        settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(settings.ledger_seed_path, settings.ledger_path)
        # WAL: readers don't wait for the writer, so concurrent invoices in a batch don't queue on
        # locks (M7). Set once on the runtime copy only; the committed seed stays untouched.
        with sqlite3.connect(settings.ledger_path) as db:
            db.execute("PRAGMA journal_mode=WAL")
    return LedgerRepo(settings.ledger_path)
