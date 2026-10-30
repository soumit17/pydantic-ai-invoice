"""Harness guards on the poster (M8, spec §10.3): ApprovalReviewer approves, ToolCallJudge blocks."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from invoiceiq.agents.poster import PostingContext, build_poster
from invoiceiq.approvals import request_posting, resolve
from invoiceiq.capabilities.guards import JUDGE_QUESTION, REVIEW_QUESTION, REVIEWER_USER, CostRecordingModel
from invoiceiq.pipeline.graph import InvoiceSource, PipelineDeps, pipeline_deps, run_pipeline
from invoiceiq.repos.app_store import AppStore
from invoiceiq.repos.ledger import open_ledger
from tests.fakes import fake_auditor, fake_extractor, fake_guard, fake_poster, runtime_settings, truth

HIGH_VALUE = "inv-017"  # clean, above ₹50k: the reviewer's tier
PRICE_DRIFT = "inv-024"  # awaiting_approval *with* a finding: never reviewed
HELD = "inv-028"  # blocked vendor


def is_review(text: str) -> bool:
    return REVIEW_QUESTION in text


def is_judgement(text: str) -> bool:
    return JUDGE_QUESTION in text


def overposting(text: str) -> bool:
    """What the judge is asked, answered exactly: is the call's amount > the requested total + 1?"""
    total = Decimal(re.findall(r"user: Post our document .*?, [A-Z]{3} ([\d.]+)\.", text)[0])
    call = json.loads(text.split("<tool_call>", 1)[1].split("</tool_call>", 1)[0])
    return Decimal(call["arguments"]["amount"]) > total + 1


def honest(reviewer: str = "no"):  # type: ignore[no-untyped-def]
    def answer(text: str) -> str:
        if is_review(text):
            return reviewer
        assert is_judgement(text)
        return "yes" if overposting(text) else "no"

    return answer


async def run(dataset: Path, tmp_path: Path, file_id: str, answer, prompts: list[str]) -> PipelineDeps:  # type: ignore[no-untyped-def]
    gt = truth(dataset, file_id)
    settings = runtime_settings(dataset, tmp_path)
    deps = await pipeline_deps(
        settings, extractor=fake_extractor(settings, gt.expected), auditor=fake_auditor(settings),
        poster=fake_poster(settings, guards=fake_guard(answer, prompts)),
    )  # fmt: skip
    await run_pipeline(InvoiceSource(file_id, dataset / "invoices" / gt.file_name), deps)
    return deps


def postings(deps: PipelineDeps) -> list[tuple[str, str]]:
    with sqlite3.connect(deps.ledger.path) as db:
        rows = db.execute("SELECT total, posted_by FROM posted_invoices WHERE posted_by != 'seed'").fetchall()
    return [(f"{total:.2f}", by) for total, by in rows]


# --- ApprovalReviewer -----------------------------------------------------------------------------


async def test_reviewer_posts_the_high_value_clean_tier(dataset: Path, tmp_path: Path) -> None:
    prompts: list[str] = []
    deps = await run(dataset, tmp_path, HIGH_VALUE, honest("no"), prompts)
    record = await deps.store.get_invoice(HIGH_VALUE)
    assert record is not None and record.status == "posted" and record.receipt is not None
    assert record.decision is not None and record.decision.outcome == "awaiting_approval"  # policy unchanged
    assert postings(deps) == [("239538.72", REVIEWER_USER)]  # the ledger says who approved it
    assert await deps.store.get_pending(HIGH_VALUE) is None  # nothing left for a person
    assert [is_review(p) for p in prompts] == [True, False]  # reviewed, then judged as it ran


async def test_reviewer_saying_yes_leaves_it_for_a_person(dataset: Path, tmp_path: Path) -> None:
    prompts: list[str] = []
    deps = await run(dataset, tmp_path, HIGH_VALUE, honest("yes"), prompts)
    record = await deps.store.get_invoice(HIGH_VALUE)
    assert record is not None and record.status == "awaiting_approval" and postings(deps) == []
    resolution = await resolve(
        HIGH_VALUE, approve=True, user="asha", poster=deps.poster, store=deps.store, ledger=deps.ledger
    )
    assert resolution.status == "posted" and postings(deps) == [("239538.72", "asha")]


async def test_reviewer_never_sees_invoices_with_findings(dataset: Path, tmp_path: Path) -> None:
    for file_id, status in [(PRICE_DRIFT, "awaiting_approval"), (HELD, "held")]:
        prompts: list[str] = []
        deps = await run(dataset, tmp_path / file_id, file_id, honest("no"), prompts)
        record = await deps.store.get_invoice(file_id)
        assert record is not None and record.status == status and prompts == []


# --- ToolCallJudge --------------------------------------------------------------------------------


async def test_judge_allows_a_partial_posting(dataset: Path, tmp_path: Path) -> None:
    prompts: list[str] = []
    deps = await run(dataset, tmp_path, HIGH_VALUE, honest("yes"), prompts)  # parked for a person
    resolution = await resolve(
        HIGH_VALUE, approve=True, user="asha", amount=Decimal("1000.00"),
        poster=deps.poster, store=deps.store, ledger=deps.ledger,
    )  # fmt: skip
    assert resolution.status == "posted" and postings(deps) == [("1000.00", "asha")]
    assert '"amount": "1000.00"' in prompts[-1]  # the judge saw the approver's edited amount


def overposting_poster_model() -> FunctionModel:
    """A poster model gone wrong: asks to post ten times the invoice total."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        if isinstance(last, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in last.parts):
            ret = next(p for p in last.parts if isinstance(p, ToolReturnPart))
            return ModelResponse(parts=[TextPart(str(ret.content))])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "post_to_ledger", {"invoice_ref": "inv-x", "amount": "600000", "idempotency_key": "k"}
                )
            ]
        )

    return FunctionModel(fn)


async def test_judge_blocks_overposting_even_after_approval(dataset: Path, tmp_path: Path) -> None:
    settings = runtime_settings(dataset, tmp_path)
    prompts: list[str] = []
    poster = build_poster(settings, model=overposting_poster_model(), guards=fake_guard(honest(), prompts))
    store, ledger = await AppStore.open(settings), open_ledger(settings)
    context = PostingContext(
        invoice_ref="inv-x", vendor_id="V001", vendor_name="Acme", invoice_no="A/1",
        invoice_date=date(2026, 10, 1), amount=Decimal("60000.00"), currency="INR",
        content_hash="k", idempotency_key="k", decision="auto_approved",
    )  # fmt: skip
    assert await request_posting(context, poster=poster, store=store, ledger=ledger) is None  # parked
    resolution = await resolve(
        "inv-x", approve=True, user="policy", poster=poster, store=store, ledger=ledger
    )
    assert resolution.status == "denied" and resolution.receipt is None
    assert resolution.message is not None and "amount judge blocked" in resolution.message
    assert await ledger.get_posting("k") is None
    assert len(prompts) == 1 and is_judgement(prompts[0])  # auto_approved: judged, never reviewed


async def test_a_failing_judge_fails_open(dataset: Path, tmp_path: Path) -> None:
    # on_uncertain="allow": post_to_ledger's own amount check is the hard boundary (D23).
    def broken(text: str) -> str:
        if is_judgement(text):
            raise RuntimeError("judge is down")
        return "yes"

    deps = await run(dataset, tmp_path, HIGH_VALUE, broken, [])
    resolution = await resolve(
        HIGH_VALUE, approve=True, user="asha", poster=deps.poster, store=deps.store, ledger=deps.ledger
    )
    assert resolution.status == "posted"


# --- cost: the guards' own requests ---------------------------------------------------------------


async def test_guard_requests_reach_the_cost_ledger_only_through_the_wrapper(
    dataset: Path, tmp_path: Path
) -> None:
    # The reviewer/judge agents are built inside the Harness capabilities, so the poster's CostHooks
    # never see their requests (D23). CostRecordingModel records them under its own label.
    settings = runtime_settings(dataset, tmp_path)

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"response": "no"})],
            usage=RequestUsage(input_tokens=300, output_tokens=5),
            model_name="claude-haiku-5-5",
        )

    guards = CostRecordingModel(FunctionModel(answer), settings=settings, label="guards")
    gt = truth(dataset, HIGH_VALUE)
    deps = await pipeline_deps(
        settings, extractor=fake_extractor(settings, gt.expected), auditor=fake_auditor(settings),
        poster=fake_poster(settings, guards=guards),
    )  # fmt: skip
    await run_pipeline(InvoiceSource(HIGH_VALUE, dataset / "invoices" / gt.file_name), deps)
    lines = [json.loads(line) for line in settings.costs_path.read_text().splitlines()]
    assert [ln["label"] for ln in lines if ln["label"] in ("guards", "poster")].count("guards") == 2
