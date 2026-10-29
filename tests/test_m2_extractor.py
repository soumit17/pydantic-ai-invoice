"""Extractor tests with FunctionModel: we script exactly what the "model" returns on each request."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic_ai import BinaryContent, capture_run_messages
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.settings import ModelSettings

from invoiceiq.agents.extractor import FAILURE_TOOL, INVOICE_TOOL, ExtractDeps, build_extractor
from invoiceiq.config import Settings
from invoiceiq.models import ExtractionFailure, RawInvoice
from invoiceiq.pipeline.documents import load_document
from invoiceiq.scoring import score
from tests.test_m1_domain import US, WB_VENDOR, invoice, line

DEPS = ExtractDeps(company_gstin=US, today=date(2026, 10, 29))


def invoice_args(**changes: Any) -> dict[str, Any]:
    data = invoice().model_dump(mode="json")
    for key, value in changes.items():
        target = data
        *path, last = key.split(".")
        for p in path:
            target = target[p]
        target[last] = value
    return data


def scripted(
    *responses: dict[str, Any] | tuple[str, dict[str, Any]],
) -> tuple[FunctionModel, list[AgentInfo]]:
    """A FunctionModel that returns the given output-tool calls in order, recording what it was given."""
    seen: list[AgentInfo] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        item = responses[min(len(seen), len(responses)) - 1]
        tool, args = item if isinstance(item, tuple) else (INVOICE_TOOL, item)
        return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])

    return FunctionModel(fn), seen


def run(settings: Settings, model: FunctionModel, prompt: str = "doc") -> RawInvoice | ExtractionFailure:
    agent = build_extractor(settings, model=model)
    return agent.run_sync(prompt, deps=DEPS).output


# --- Union output routing -----------------------------------------------------------------------


def test_union_output_has_one_tool_per_member(settings: Settings) -> None:
    model, seen = scripted(invoice_args())
    run(settings, model)
    assert sorted(t.name for t in seen[0].output_tools) == [FAILURE_TOOL, INVOICE_TOOL]
    assert not seen[0].allow_text_output  # the model must pick a tool


def test_invoice_tool_returns_raw_invoice(settings: Settings) -> None:
    model, _ = scripted(invoice_args())
    out = run(settings, model)
    assert isinstance(out, RawInvoice) and out.invoice_no == "INV/26/042"


def test_failure_tool_returns_extraction_failure(settings: Settings) -> None:
    failure = {"reason": "not_an_invoice", "detected_document_type": "quotation"}
    model, _ = scripted((FAILURE_TOOL, failure))
    out = run(settings, model)
    assert isinstance(out, ExtractionFailure) and out.detected_document_type == "quotation"


# --- Retries ------------------------------------------------------------------------------------


def test_wrong_buyer_gstin_triggers_model_retry(settings: Settings) -> None:
    model, seen = scripted(invoice_args(**{"buyer.gstin": WB_VENDOR}), invoice_args())
    agent = build_extractor(settings, model=model)
    with capture_run_messages() as messages:
        result = agent.run_sync("doc", deps=DEPS)
    assert isinstance(result.output, RawInvoice) and result.output.buyer.gstin == US
    assert len(seen) == 2
    # In V2 the retry goes back to the model as the output tool's *return* value, carrying our
    # ModelRetry message, rather than as a separate RetryPromptPart.
    returns = [
        p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)
    ]
    assert "buyer GSTIN should be ours" in str(returns[0].content)


def test_schema_violation_is_fed_back_and_retried(settings: Settings) -> None:
    model, seen = scripted(invoice_args(**{"supplier.gstin": "NOT-A-GSTIN"}), invoice_args())
    out = run(settings, model)
    assert isinstance(out, RawInvoice) and len(seen) == 2


def test_retries_are_capped_at_two(settings: Settings) -> None:
    model, seen = scripted(invoice_args(**{"buyer.gstin": WB_VENDOR}))
    with pytest.raises(UnexpectedModelBehavior):
        run(settings, model)
    assert len(seen) == 3  # 1 attempt + 2 retries


def test_vendor_arithmetic_error_survives_extraction(settings: Settings) -> None:
    # The lenient RawInvoice lets a real vendor mistake through untouched, with no retry (§7.1).
    bad = line().model_copy(update={"taxable_value": Decimal("1450.00")})
    model, seen = scripted(invoice(bad).model_dump(mode="json"))
    out = run(settings, model)
    assert isinstance(out, RawInvoice) and out.line_items[0].taxable_value == Decimal("1450.00")
    assert len(seen) == 1


# --- Instructions -------------------------------------------------------------------------------


def test_dynamic_instructions_carry_deps(settings: Settings) -> None:
    model, seen = scripted(invoice_args())
    run(settings, model)
    instructions = seen[0].instructions or ""
    assert "Copy every number exactly as printed" in instructions  # static
    assert f"its GSTIN is {US}" in instructions and "29 October 2026" in instructions  # dynamic


def test_all_output_modes_build(settings: Settings) -> None:
    for mode in ("tool", "native", "prompted"):
        assert build_extractor(settings, output_mode=mode).name == "extractor"


# --- Documents ----------------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_pdf_goes_in_as_binary_content(tmp_path: Path) -> None:
    [_, doc] = load_document(_write(tmp_path, "a.pdf", b"%PDF-1.4 fake"))
    assert isinstance(doc, BinaryContent) and doc.media_type == "application/pdf"


def test_large_scans_are_downscaled(tmp_path: Path) -> None:
    buf = io.BytesIO()
    Image.new("L", (3200, 2000), 255).save(buf, format="PNG")
    [_, doc] = load_document(_write(tmp_path, "a.png", buf.getvalue()))
    assert isinstance(doc, BinaryContent)
    assert max(Image.open(io.BytesIO(doc.data)).size) == 1600


def test_email_goes_in_as_text(tmp_path: Path) -> None:
    msg = EmailMessage()
    msg["From"], msg["Subject"] = "v@x.example", "Invoice 42"
    msg.set_content("Invoice No: 42\nTotal: Rs.100")
    [text] = load_document(_write(tmp_path, "a.eml", msg.as_bytes()))
    assert isinstance(text, str) and "Subject: Invoice 42" in text and "Invoice No: 42" in text


# --- Scoring ------------------------------------------------------------------------------------


def test_score_counts_wrong_fields() -> None:
    expected = invoice(line(), line("50.00"))
    got = expected.model_copy(update={"invoice_no": "INV-26-042", "total": expected.total + 5})
    s = score(expected, got)
    assert s.kind_ok and s.total == 10 + 2 * 9
    assert s.wrong == [f"total: want {expected.total!r}, got {got.total!r}"]  # number formatting is ignored


def test_score_wrong_kind() -> None:
    s = score(invoice(), ExtractionFailure(reason="unreadable"))
    assert not s.kind_ok and s.accuracy == 0


class SettingsSpy(FunctionModel):
    """Records the merged settings the model is actually called with. (AgentInfo.model_settings only
    shows the agent's own settings, not those contributed by capabilities like Thinking and Caching.)"""

    sent: list[ModelSettings | None]

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self.sent.append(model_settings)
        return await super().request(messages, model_settings, model_request_parameters)


def test_thinking_off_caching_on_no_sampling_settings(settings: Settings) -> None:
    model = SettingsSpy(
        lambda messages, info: ModelResponse(parts=[ToolCallPart(INVOICE_TOOL, invoice_args())])
    )
    model.sent = []
    run(settings, model)
    sent = model.sent[0] or {}
    assert sent.get("thinking") is False  # Thinking(effort=False)
    assert sent.get("cache") == {"messages": False}  # Caching(messages=False)
    assert "temperature" not in sent  # Haiku 5.5 rejects sampling settings
    assert sent.get("max_tokens") == settings.max_tokens_default
