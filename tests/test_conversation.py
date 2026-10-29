"""Ask-AP: Conversation memory in SQLite, a usage limit spanning turns, append-only instructions, tools."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    InstructionDeltaPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import UsageLimits

from invoiceiq.agents.ask_ap import build_ask_ap, chat
from invoiceiq.config import Settings
from invoiceiq.models import Decision, InvoiceRecord
from invoiceiq.repos.app_store import AppStore
from invoiceiq.repos.ledger import open_ledger
from tests.fakes import AS_OF, fake_ask_ap, runtime_settings


async def setup(dataset: Path, root: Path) -> tuple[Settings, AppStore]:
    settings = runtime_settings(dataset, root)
    return settings, await AppStore.open(settings)


async def test_three_turns_remember_and_persist(dataset: Path, tmp_path: Path) -> None:
    settings, store = await setup(dataset, tmp_path)
    agent, ledger = fake_ask_ap(settings), open_ledger(settings)
    kw = {"agent": agent, "store": store, "ledger": ledger, "today": AS_OF}
    one = await chat("first?", conversation_id=None, user="alice", **kw)  # pyright: ignore[reportArgumentType]
    await chat("second?", conversation_id=one.conversation_id, user="alice", **kw)  # pyright: ignore[reportArgumentType]
    three = await chat("third?", conversation_id=one.conversation_id, user="alice", **kw)  # pyright: ignore[reportArgumentType]
    assert three.reply == "first? | second? | third?"
    stored = await store.get_conversation(one.conversation_id)  # round-tripped through SQLite
    assert stored is not None and stored.usage.requests == 3 and len(stored.messages) == 6


async def test_usage_limit_spans_the_whole_conversation(dataset: Path, tmp_path: Path) -> None:
    settings, store = await setup(dataset, tmp_path)
    kw = {"agent": fake_ask_ap(settings), "store": store, "ledger": open_ledger(settings), "today": AS_OF,
          "usage_limits": UsageLimits(request_limit=2)}  # fmt: skip
    one = await chat("a", conversation_id=None, user="alice", **kw)  # pyright: ignore[reportArgumentType]
    await chat("b", conversation_id=one.conversation_id, user="alice", **kw)  # pyright: ignore[reportArgumentType]
    with pytest.raises(UsageLimitExceeded):  # turn 3 alone is 1 request, but the conversation is at 2
        await chat("c", conversation_id=one.conversation_id, user="alice", **kw)  # pyright: ignore[reportArgumentType]
    fresh = await chat("new thread", conversation_id=None, user="alice", **kw)  # pyright: ignore[reportArgumentType]
    assert fresh.requests_used == 1  # a new conversation has its own budget


async def test_user_change_is_appended_not_rewritten(dataset: Path, tmp_path: Path) -> None:
    settings, store = await setup(dataset, tmp_path)
    kw = {"agent": fake_ask_ap(settings), "store": store, "ledger": open_ledger(settings), "today": AS_OF}
    one = await chat("hi", conversation_id=None, user="alice", **kw)  # pyright: ignore[reportArgumentType]
    await chat("hi again", conversation_id=one.conversation_id, user="bob", **kw)  # pyright: ignore[reportArgumentType]
    stored = await store.get_conversation(one.conversation_id)
    assert stored is not None
    requests = [m for m in stored.messages if isinstance(m, ModelRequest)]
    deltas = [p for m in requests for p in m.parts if isinstance(p, InstructionDeltaPart)]
    assert len(deltas) == 1 and "Current user: bob" in str(deltas[0].content)
    # The instruction prefix is still alice's: changing it would invalidate the cached prefix.
    assert all("Current user: alice" in (m.instructions or "") for m in requests)


def scripted_tool_then_answer(tool: str, args: dict[str, object]) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        returned = (
            [p for p in last.parts if isinstance(p, ToolReturnPart)] if isinstance(last, ModelRequest) else []
        )
        if not returned:
            return ModelResponse(parts=[ToolCallPart(tool, args)])
        return ModelResponse(parts=[TextPart(returned[0].model_response_str())])

    return FunctionModel(fn)


async def test_vendor_spend_sums_in_code(dataset: Path, tmp_path: Path) -> None:
    settings, store = await setup(dataset, tmp_path)
    model = scripted_tool_then_answer(
        "vendor_spend", {"vendor_id": "V001", "start": "2026-01-01", "end": "2026-12-31"}
    )
    agent = build_ask_ap(settings, model=model)
    ledger = open_ledger(settings)
    reply = await chat(
        "spend?", conversation_id=None, user="a", agent=agent, store=store, ledger=ledger, today=AS_OF
    )
    with sqlite3.connect(settings.ledger_path) as db:
        (expected,) = db.execute(
            "SELECT sum(total) FROM posted_invoices "
            "WHERE vendor_id='V001' AND invoice_date BETWEEN '2026-01-01' AND '2026-12-31'"
        ).fetchone()
    assert f'"INR":"{Decimal(str(expected)).quantize(Decimal("0.01"))}"' in reply.reply.replace(" ", "")


async def test_explain_invoice_reads_the_stored_record(dataset: Path, tmp_path: Path) -> None:
    settings, store = await setup(dataset, tmp_path)
    decision = Decision(outcome="held", reasons=["critical: blocked_vendor: vendor X is blocked"])
    await store.save_invoice(
        InvoiceRecord(invoice_id="inv-028", file_name="inv-028.pdf", status="held", decision=decision)
    )
    agent = build_ask_ap(
        settings, model=scripted_tool_then_answer("explain_invoice", {"invoice_id": "inv-028"})
    )
    reply = await chat("why held?", conversation_id=None, user="a", agent=agent, store=store,
                       ledger=open_ledger(settings), today=date(2026, 10, 29))  # fmt: skip
    assert "blocked_vendor" in reply.reply and '"status":"held"' in reply.reply.replace(" ", "")


async def test_ask_ap_reuses_the_ledger_capability(dataset: Path, tmp_path: Path) -> None:
    settings, store = await setup(dataset, tmp_path)
    seen: list[AgentInfo] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(parts=[TextPart("ok")])

    agent = build_ask_ap(settings, model=FunctionModel(fn))
    await chat(
        "hi",
        conversation_id=None,
        user="a",
        agent=agent,
        store=store,
        ledger=open_ledger(settings),
        today=AS_OF,
    )
    tools = {t.name for t in seen[0].function_tools}
    assert {"get_vendor", "get_po", "vendor_history", "search_posted"} <= tools  # from ledger_capability
    assert {"explain_invoice", "find_vendors", "vendor_spend", "list_invoices"} <= tools
