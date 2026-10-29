"""The ledger MCP server, tested in memory: as a raw MCP toolset, and through Ask-AP's MCP capability."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from invoiceiq.agents.ask_ap import build_ask_ap, chat
from invoiceiq.config import Settings
from invoiceiq.mcp_server import build_mcp_server
from invoiceiq.models import Decision, InvoiceRecord
from invoiceiq.repos.app_store import AppStore
from invoiceiq.repos.ledger import open_ledger
from tests.fakes import AS_OF, runtime_settings

LEDGER_TOOLS = {"find_vendor", "get_purchase_order", "vendor_spend", "list_flagged_invoices"}


async def call(settings: Settings, tool: str, args: dict[str, Any]) -> Any:
    # MCPToolset accepts the server object itself: an in-memory transport, no process or port.
    async with MCPToolset(build_mcp_server(settings)) as toolset:
        return await toolset.direct_call_tool(tool, args)


# --- the server on its own ----------------------------------------------------------------------


async def test_server_lists_its_tools_and_the_policy_resource(dataset: Path, tmp_path: Path) -> None:
    settings = runtime_settings(dataset, tmp_path)
    async with MCPToolset(build_mcp_server(settings)) as toolset:
        assert {t.name for t in await toolset.list_tools()} == LEDGER_TOOLS
        assert [str(r.uri) for r in await toolset.list_resources()] == ["ledger://policy"]
        policy = await toolset.read_resource("ledger://policy")
    assert isinstance(policy, str) and "Auto-approve" in policy and "₹50000" in policy


async def test_find_vendor_by_name_or_gstin(dataset: Path, tmp_path: Path) -> None:
    settings = runtime_settings(dataset, tmp_path)
    [vendor] = await call(settings, "find_vendor", {"query": "balaji"})  # structured output: a list of dicts
    assert vendor["vendor_id"] == "V001"
    [same] = await call(settings, "find_vendor", {"query": vendor["gstin"]})
    assert same["name"] == vendor["name"]


async def test_vendor_spend_matches_the_ledger(dataset: Path, tmp_path: Path) -> None:
    settings = runtime_settings(dataset, tmp_path)
    open_ledger(settings)  # create the runtime copy so we can query it directly
    spend = await call(
        settings, "vendor_spend", {"vendor_id": "V001", "start": "2026-01-01", "end": "2026-12-31"}
    )
    with sqlite3.connect(settings.ledger_path) as db:
        (expected, n) = db.execute(
            "SELECT sum(total), count(*) FROM posted_invoices "
            "WHERE vendor_id='V001' AND invoice_date BETWEEN '2026-01-01' AND '2026-12-31'"
        ).fetchone()
    assert spend["invoices"] == n and float(spend["totals"]["INR"]) == round(expected, 2)


async def test_list_flagged_invoices_reads_the_app_store(dataset: Path, tmp_path: Path) -> None:
    settings = runtime_settings(dataset, tmp_path)
    store = await AppStore.open(settings)
    decision = Decision(outcome="held", reasons=["critical: blocked_vendor: vendor is blocked"])
    await store.save_invoice(
        InvoiceRecord(invoice_id="inv-028", file_name="inv-028.pdf", status="held", decision=decision)
    )
    rows = await call(settings, "list_flagged_invoices", {"status": "held"})
    assert [r["invoice_id"] for r in rows] == ["inv-028"] and "blocked_vendor" in rows[0]["reasons"][0]


# --- through Ask-AP's MCP capability ------------------------------------------------------------


async def test_ask_ap_reaches_the_ledger_over_mcp_with_prefixed_tools(dataset: Path, tmp_path: Path) -> None:
    settings = runtime_settings(dataset, tmp_path)
    seen: list[AgentInfo] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        last = messages[-1]
        returned = (
            [p for p in last.parts if isinstance(p, ToolReturnPart)] if isinstance(last, ModelRequest) else []
        )
        if not returned:
            span = {"vendor_id": "V001", "start": "2026-07-01", "end": "2026-09-30"}
            return ModelResponse(parts=[ToolCallPart("ledger_vendor_spend", span)])
        return ModelResponse(parts=[TextPart(returned[0].model_response_str())])

    agent = build_ask_ap(settings, model=FunctionModel(fn))  # ledger="mcp" is the default
    store = await AppStore.open(settings)
    reply = await chat(
        "What did we pay Shri Balaji last quarter?", conversation_id=None, user="alice", agent=agent,
        store=store, ledger=open_ledger(settings), today=AS_OF,
    )  # fmt: skip
    tools = {t.name for t in seen[0].function_tools}
    assert {f"ledger_{name}" for name in LEDGER_TOOLS} <= tools  # tool_prefix=True with id="ledger"
    assert "get_vendor" not in tools  # the direct ledger_capability is gone in mcp mode
    assert {"explain_invoice", "list_invoices"} <= tools  # local tools sit alongside, no collisions
    assert '"vendor_id":"V001"' in reply.reply.replace(
        " ", ""
    ) and '"start":"2026-07-01"' in reply.reply.replace(" ", "")
