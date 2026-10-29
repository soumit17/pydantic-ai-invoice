"""Application state (`data/runtime/app.sqlite`): invoice records and pending approvals.

A pending approval stores the poster's whole `Conversation` (messages, usage and the pending
deferred tool requests) as JSON. That is all a later process needs to resume the paused run.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel
from pydantic_ai import Conversation, ConversationTypeAdapter

from invoiceiq.agents.poster import PostingContext
from invoiceiq.config import Settings
from invoiceiq.models import InvoiceRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
  invoice_id  TEXT PRIMARY KEY,
  file_name   TEXT NOT NULL,
  status      TEXT NOT NULL,
  record_json TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
  conversation_id   TEXT PRIMARY KEY,
  user              TEXT NOT NULL,
  conversation_json TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_approvals (
  invoice_id        TEXT PRIMARY KEY,
  conversation_json TEXT NOT NULL,
  tool_call_ids     TEXT NOT NULL,
  context_json      TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  status            TEXT NOT NULL CHECK (status IN ('pending','resolving','approved','denied')),
  resolved_by       TEXT,
  note              TEXT,
  resolved_at       TEXT
);
"""

ApprovalStatus = Literal["pending", "resolving", "approved", "denied"]


class PendingApproval(BaseModel):
    invoice_id: str
    conversation: Conversation
    tool_call_ids: list[str]
    context: PostingContext
    created_at: str
    status: ApprovalStatus
    resolved_by: str | None = None
    note: str | None = None
    resolved_at: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AppStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    async def open(cls, settings: Settings) -> AppStore:
        settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        store = cls(settings.app_db_path)
        async with aiosqlite.connect(store.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
        return store

    async def _execute(self, sql: str, params: tuple[Any, ...]) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(sql, params)
            await db.commit()
            return cur.rowcount

    async def _fetch(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            async with db.execute(sql, params) as cur:
                return list(await cur.fetchall())

    # --- invoices ---------------------------------------------------------------------------

    async def save_invoice(self, record: InvoiceRecord) -> None:
        now = _now()
        await self._execute(
            "INSERT INTO invoices VALUES (?,?,?,?,?,?) ON CONFLICT(invoice_id) DO UPDATE SET "
            "status = excluded.status, record_json = excluded.record_json, updated_at = excluded.updated_at",
            (record.invoice_id, record.file_name, record.status, record.model_dump_json(), now, now),
        )

    async def get_invoice(self, invoice_id: str) -> InvoiceRecord | None:
        rows = await self._fetch("SELECT record_json FROM invoices WHERE invoice_id = ?", (invoice_id,))
        return InvoiceRecord.model_validate_json(rows[0]["record_json"]) if rows else None

    async def list_invoices(self, status: str | None = None, *, limit: int = 100) -> list[InvoiceRecord]:
        sql = "SELECT record_json FROM invoices"
        params: tuple[Any, ...] = ()
        if status:
            sql, params = sql + " WHERE status = ?", (status,)
        rows = await self._fetch(sql + " ORDER BY created_at DESC, invoice_id LIMIT ?", (*params, limit))
        return [InvoiceRecord.model_validate_json(r["record_json"]) for r in rows]

    async def new_invoice_id(self, preferred: str) -> str:
        """`preferred` (e.g. a file's stem) if unused, otherwise preferred-2, preferred-3, ..."""
        candidate, n = preferred, 1
        while await self.get_invoice(candidate) is not None:
            n += 1
            candidate = f"{preferred}-{n}"
        return candidate

    # --- conversations (Ask-AP) -------------------------------------------------------------

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        rows = await self._fetch(
            "SELECT conversation_json FROM conversations WHERE conversation_id = ?", (conversation_id,)
        )
        return ConversationTypeAdapter.validate_json(rows[0]["conversation_json"]) if rows else None

    async def save_conversation(self, conversation: Conversation, *, user: str) -> None:
        now = _now()
        await self._execute(
            "INSERT INTO conversations VALUES (?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET "
            "conversation_json = excluded.conversation_json, updated_at = excluded.updated_at",
            (
                conversation.conversation_id,
                user,
                ConversationTypeAdapter.dump_json(conversation).decode(),
                now,
                now,
            ),
        )

    # --- pending approvals ------------------------------------------------------------------

    async def save_pending(self, context: PostingContext, conversation: Conversation) -> None:
        requests = conversation.deferred_tool_requests
        ids = [c.tool_call_id for c in requests.approvals] if requests else []
        await self._execute(
            "INSERT INTO pending_approvals (invoice_id, conversation_json, tool_call_ids, context_json, "
            "created_at, status) VALUES (?,?,?,?,?, 'pending') ON CONFLICT(invoice_id) DO UPDATE SET "
            "conversation_json = excluded.conversation_json, tool_call_ids = excluded.tool_call_ids, "
            "status = 'pending'",
            (
                context.invoice_ref,
                # ConversationTypeAdapter: the documented way to serialize a whole Conversation.
                ConversationTypeAdapter.dump_json(conversation).decode(),
                json.dumps(ids),
                context.model_dump_json(),
                _now(),
            ),
        )

    async def get_pending(self, invoice_id: str) -> PendingApproval | None:
        rows = await self._fetch("SELECT * FROM pending_approvals WHERE invoice_id = ?", (invoice_id,))
        return self._pending(rows[0]) if rows else None

    async def list_pending(self, status: ApprovalStatus = "pending") -> list[PendingApproval]:
        rows = await self._fetch(
            "SELECT * FROM pending_approvals WHERE status = ? ORDER BY created_at, invoice_id", (status,)
        )
        return [self._pending(r) for r in rows]

    async def claim_pending(self, invoice_id: str) -> bool:
        """Atomically move pending → resolving. False if someone else got there first."""
        n = await self._execute(
            "UPDATE pending_approvals SET status = 'resolving' WHERE invoice_id = ? AND status = 'pending'",
            (invoice_id,),
        )
        return n == 1

    async def release_pending(self, invoice_id: str) -> None:
        """Undo a claim after a failed resolution, so the approval can be retried."""
        await self._execute(
            "UPDATE pending_approvals SET status = 'pending' WHERE invoice_id = ? AND status = 'resolving'",
            (invoice_id,),
        )

    async def finish_pending(
        self, invoice_id: str, *, status: Literal["approved", "denied"], by: str, note: str | None
    ) -> None:
        await self._execute(
            "UPDATE pending_approvals SET status = ?, resolved_by = ?, note = ?, resolved_at = ? "
            "WHERE invoice_id = ?",
            (status, by, note, _now(), invoice_id),
        )

    @staticmethod
    def _pending(row: sqlite3.Row) -> PendingApproval:
        return PendingApproval(
            invoice_id=row["invoice_id"],
            conversation=ConversationTypeAdapter.validate_json(row["conversation_json"]),
            tool_call_ids=json.loads(row["tool_call_ids"]),
            context=PostingContext.model_validate_json(row["context_json"]),
            created_at=row["created_at"],
            status=row["status"],
            resolved_by=row["resolved_by"],
            note=row["note"],
            resolved_at=row["resolved_at"],
        )
