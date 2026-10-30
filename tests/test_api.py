"""API tests: httpx against the ASGI app, with FunctionModel-backed agents injected as services."""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from invoiceiq.api import Services, create_app
from invoiceiq.pipeline.events import EventBus
from invoiceiq.pipeline.graph import pipeline_deps
from tests.fakes import fake_ask_ap, fake_auditor, fake_extractor, fake_poster, runtime_settings, truth


async def make_client(dataset: Path, root: Path, file_id: str) -> tuple[httpx.AsyncClient, Services]:
    """An app whose extractor answers with `file_id`'s ground truth; that invoice is copied into data/."""
    gt = truth(dataset, file_id)
    settings = runtime_settings(dataset, root)
    (settings.data_dir / "invoices").mkdir(exist_ok=True)
    shutil.copyfile(dataset / "invoices" / gt.file_name, settings.data_dir / "invoices" / gt.file_name)
    events = EventBus()
    pipeline = await pipeline_deps(
        settings, extractor=fake_extractor(settings, gt.expected), auditor=fake_auditor(settings),
        poster=fake_poster(settings),
    )  # fmt: skip
    pipeline.events = events
    services = Services(settings=settings, pipeline=pipeline, ask=fake_ask_ap(settings), events=events)
    app = create_app(services=services)  # services injected: no lifespan, no real models
    # ASGITransport runs the whole request, background task included, before returning.
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, services


@pytest.fixture
async def client(dataset: Path, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    c, _ = await make_client(dataset, tmp_path, "inv-017")  # clean, above ₹50k → needs a human
    async with c:
        yield c


async def sse_events(client: httpx.AsyncClient, url: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    async with client.stream("GET", url) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


# --- invoices -----------------------------------------------------------------------------------


async def test_ingest_by_path_then_fetch_the_record(client: httpx.AsyncClient) -> None:
    r = await client.post("/invoices", json={"path": "inv-017.pdf"})  # relative to the project...
    assert r.status_code == 422  # ...is outside data/, so refused
    accepted = await client.post("/invoices", json={"path": str(await _data_file(client, "inv-017.pdf"))})
    assert accepted.status_code == 202
    body = accepted.json()
    assert body == {"invoice_id": "inv-017", "status": "processing", "stream": "/invoices/inv-017/stream"}
    record = (await client.get("/invoices/inv-017")).json()
    assert record["status"] == "awaiting_approval" and record["decision"]["outcome"] == "awaiting_approval"


async def test_stream_shows_steps_and_partial_json_growing(client: httpx.AsyncClient) -> None:
    await client.post("/invoices", json={"path": str(await _data_file(client, "inv-017.pdf"))})
    events = await sse_events(client, "/invoices/inv-017/stream")
    steps = [e["node"] for e in events if e["type"] == "step"]
    partials = [e["invoice"] for e in events if e["type"] == "partial"]
    assert steps[:2] == ["load_document", "extract"] and "audit" in steps and "park" in steps
    assert len(partials) >= 2
    sizes = [len(json.dumps(p)) for p in partials]
    assert sizes == sorted(sizes) and sizes[0] < sizes[-1]  # the JSON grows
    assert isinstance(partials[-1], dict) and partials[-1]["invoice_no"] == "288"
    assert events[-1] == {"type": "done", "status": "awaiting_approval"}


async def test_multipart_upload(dataset: Path, tmp_path: Path) -> None:
    client, services = await make_client(dataset, tmp_path, "inv-001")
    async with client:
        pdf = (dataset / "invoices" / "inv-001.pdf").read_bytes()
        r = await client.post("/invoices", files={"file": ("vendor-scan.pdf", pdf, "application/pdf")})
        assert r.status_code == 202, r.text
        assert r.json()["invoice_id"] == "vendor-scan"
        assert (services.settings.runtime_dir / "uploads" / "vendor-scan.pdf").read_bytes() == pdf
        assert (await client.get("/invoices/vendor-scan")).json()["status"] == "posted"
        bad = await client.post("/invoices", files={"file": ("notes.txt", b"hello", "text/plain")})
        assert bad.status_code == 422


async def test_unknown_invoice_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/invoices/nope")).status_code == 404
    assert (await client.get("/invoices/nope/stream")).status_code == 404


# --- approvals ----------------------------------------------------------------------------------


async def test_approval_inbox_and_decisions(client: httpx.AsyncClient) -> None:
    await client.post("/invoices", json={"path": str(await _data_file(client, "inv-017.pdf"))})
    [item] = (await client.get("/approvals")).json()
    assert item["invoice_id"] == "inv-017" and item["waiting_on"][0]["tool"] == "post_to_ledger"

    no_user = await client.post("/approvals/inv-017", json={"decision": "approve"})
    assert no_user.status_code == 422  # X-User is required
    ok = await client.post("/approvals/inv-017", json={"decision": "approve"}, headers={"X-User": "alice"})
    assert ok.status_code == 200 and ok.json()["status"] == "posted" and ok.json()["posted_by"] == "alice"
    again = await client.post("/approvals/inv-017", json={"decision": "approve"}, headers={"X-User": "bob"})
    assert again.json()["already_resolved"] is True and again.json()["posted_by"] == "alice"
    assert (await client.get("/approvals")).json() == []
    missing = await client.post("/approvals/inv-999", json={"decision": "approve"}, headers={"X-User": "a"})
    assert missing.status_code == 404


async def test_held_invoice_needs_a_note(dataset: Path, tmp_path: Path) -> None:
    client, services = await make_client(dataset, tmp_path, "inv-028")  # blocked vendor → held
    async with client:
        await client.post(
            "/invoices", json={"path": str(services.settings.data_dir / "invoices" / "inv-028.pdf")}
        )
        r = await client.post("/approvals/inv-028", json={"decision": "approve"}, headers={"X-User": "alice"})
        assert r.status_code == 409 and "note" in r.json()["detail"]
        r = await client.post(
            "/approvals/inv-028",
            json={"decision": "approve", "note": "unblocked"},
            headers={"X-User": "alice"},
        )
        assert r.json()["status"] == "posted"


# --- chat, costs, misc --------------------------------------------------------------------------


async def test_chat_remembers_three_turns(client: httpx.AsyncClient) -> None:
    alice = {"X-User": "alice"}  # same user every turn (a change of user is appended to the history)
    first = (await client.post("/chat", json={"message": "who is V001?"}, headers=alice)).json()
    cid = first["conversation_id"]
    await client.post("/chat", json={"message": "and their spend?", "conversation_id": cid}, headers=alice)
    third = (
        await client.post(
            "/chat", json={"message": "what did I ask first?", "conversation_id": cid}, headers=alice
        )
    ).json()
    assert third["reply"] == "who is V001? | and their spend? | what did I ask first?"
    assert third["conversation_id"] == cid and third["requests_used"] == 3
    unknown = await client.post("/chat", json={"message": "hi", "conversation_id": "nope"})
    assert unknown.status_code == 404


async def test_costs_health_and_batches(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"ok": True}
    costs = (await client.get("/costs")).json()
    assert costs["cap_usd"] == "6.0" and "total_usd" in costs
    # Test apps don't run the DBOS worker (the crash test covers batches end to end).
    assert (await client.post("/batches", json={"invoice_ids": "all"})).status_code == 503


async def _data_file(client: httpx.AsyncClient, name: str) -> Path:
    app = client._transport.app  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
    return app.state.services.settings.data_dir / "invoices" / name  # pyright: ignore[reportUnknownMemberType]
