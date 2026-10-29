"""CostHooks and PIIRedactionHooks, attached to the real extractor but driven by FunctionModel."""

from __future__ import annotations

import json
import logging
from datetime import date

import logfire
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from invoiceiq.agents.extractor import INVOICE_TOOL, ExtractDeps, build_extractor
from invoiceiq.capabilities.pii import redact, scrubbing_options
from invoiceiq.config import Settings
from invoiceiq.shared.cost import BudgetExceeded
from tests.test_m1_domain import US, invoice

DEPS = ExtractDeps(company_gstin=US, today=date(2026, 10, 29))
ACCOUNT = "A/c No: 467527742581"


def echo_model(seen_prompts: list[str]) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for m in messages:
            if isinstance(m, ModelRequest):
                seen_prompts.extend(str(p.content) for p in m.parts if isinstance(p, UserPromptPart))
        return ModelResponse(parts=[ToolCallPart(INVOICE_TOOL, invoice().model_dump(mode="json"))])

    return FunctionModel(fn)


# --- CostHooks ----------------------------------------------------------------------------------


def test_cost_hook_records_every_model_response(settings: Settings) -> None:
    agent = build_extractor(settings, model=echo_model([]))
    agent.run_sync("doc", deps=DEPS)
    rows = [json.loads(x) for x in settings.costs_path.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["label"] == "extractor"


def test_budget_cap_stops_the_run_before_any_request(settings: Settings) -> None:
    settings.costs_path.parent.mkdir(parents=True)
    settings.costs_path.write_text(json.dumps({"project": "p1", "usd": "99"}) + "\n")
    seen: list[str] = []
    agent = build_extractor(settings, model=echo_model(seen))
    with pytest.raises(BudgetExceeded):
        agent.run_sync("doc", deps=DEPS)
    assert seen == []  # the model was never called


# --- PII ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        (ACCOUNT, "A/c No: [REDACTED-ACCOUNT]"),
        ("Account number 123456789", "Account number [REDACTED-ACCOUNT]"),
        ("Phone: +91 9738729711", "Phone: [REDACTED-PHONE]"),
        ("Phone: +1 510 555 1234", "Phone: [REDACTED-PHONE]"),
        ("Invoice total 123456789012", "Invoice total 123456789012"),  # no A/c nearby: untouched
        ("GSTIN 19ABCDE1234F1Z5, PO-2026-0001", "GSTIN 19ABCDE1234F1Z5, PO-2026-0001"),
    ],
)
def test_redact(text: str, want: str) -> None:
    assert redact(text) == want


def test_pii_hook_redacts_the_log_but_not_the_model_input(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[str] = []
    agent = build_extractor(settings, model=echo_model(seen))
    with caplog.at_level(logging.INFO, logger="invoiceiq.audit"):
        agent.run_sync(f"Invoice from email. Bank: SBI, {ACCOUNT}", deps=DEPS)
    assert any("467527742581" in p for p in seen)  # the model saw the real document
    audit = "\n".join(r.getMessage() for r in caplog.records if r.name == "invoiceiq.audit")
    assert "[REDACTED-ACCOUNT]" in audit and "467527742581" not in audit


def test_logfire_scrubbing_masks_pii_inside_span_attributes() -> None:
    exporter = InMemorySpanExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        scrubbing=scrubbing_options(),
        distributed_tracing=True,  # as in telemetry.py; this configure() is global for later tests
        additional_span_processors=[SimpleSpanProcessor(exporter)],
    )
    with logfire.span("model request", prompt=f"Bank: SBI, {ACCOUNT}", api_key="sk-secret"):
        pass
    [span] = [s for s in exporter.get_finished_spans() if s.name == "model request"]
    attrs = dict(span.attributes or {})
    assert attrs["prompt"] == "Bank: SBI, A/c No: [REDACTED-ACCOUNT]"  # just the number is masked
    assert "sk-secret" not in str(attrs["api_key"])  # Logfire's default patterns still apply
