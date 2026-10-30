"""Extractor agent (spec §7.1): document in, `RawInvoice | ExtractionFailure` out."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic_ai import Agent, ModelRetry, NativeOutput, PromptedOutput, RunContext, ToolOutput
from pydantic_ai.capabilities import Caching, Thinking
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.models import Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from invoiceiq.capabilities.cost import build_cost_hooks
from invoiceiq.capabilities.pii import build_pii_hooks
from invoiceiq.config import Settings
from invoiceiq.models import ExtractionFailure, RawInvoice
from invoiceiq.reliability import build_model

OutputMode = Literal["tool", "native", "prompted"]

# Verb-phrase tool names: in the first runs Haiku twice called "invoke" instead of "invoice".
INVOICE_TOOL = "submit_invoice"
FAILURE_TOOL = "report_failure"
ExtractorAgent = Agent["ExtractDeps", RawInvoice | ExtractionFailure]

# One page is ~3-5k tokens in and, with thinking off, under 1k out; this leaves room for two retries.
EXTRACT_LIMITS = UsageLimits(request_limit=4, total_tokens_limit=20_000)


@dataclass
class ExtractDeps:
    company_gstin: str
    today: date


INSTRUCTIONS = """\
You extract data from Indian GST invoices for an accounts-payable team.

Copy every number exactly as printed. Never compute, correct or "fix" a figure, even when it looks
wrong: downstream checks exist to catch vendor errors, and they rely on seeing them.

Fields:
- supplier: the party that issued the document. buyer: the "Bill To" party.
- GSTIN: 15 characters, upper case. Leave it null if none is printed (foreign suppliers).
- Dates are usually DD/MM/YYYY (or like 06-Oct-2026); return them as ISO dates.
- line_items: every line, in order. description is the item text only: leave out codes printed
  beside it, such as "(HSN 4819)". taxable_value is the line amount before tax.
- gst_rate: the percentage as a whole number (18 for 18%). A printed 0% is 0; use null only
  when no GST rate is printed at all (e.g. an import invoice).
- cgst / sgst / igst: the per-line tax amounts as printed; 0 when that tax is not charged.
  If one column shows "CGST+SGST" as "a + b", cgst is a and sgst is b.
- subtotal: the taxable total; total: the grand total payable.
- total_tax: the total GST. If it is not printed as one figure, add up the printed CGST, SGST
  and IGST totals. That is the only arithmetic you may do.
- currency: INR unless the document says otherwise (e.g. USD on an import invoice).
- confidence: your honest estimate (0-1) that every field is correct.

If the document is not an invoice (a quotation, purchase order, delivery challan or receipt),
return an extraction failure with reason not_an_invoice and the detected document type.
If it is an invoice but critical fields are unreadable, return a failure and list them.
"""


def _output_type(mode: OutputMode) -> OutputSpec[RawInvoice | ExtractionFailure]:
    # A union output: with tool output, each member becomes its own output tool, so the model
    # *chooses* invoice vs failure by which tool it calls. Native and prompted output wrap the same
    # union into one JSON schema instead (compared in the M2 experiment).
    if mode == "tool":
        return [
            ToolOutput(RawInvoice, name=INVOICE_TOOL, description="Return the extracted invoice."),
            ToolOutput(ExtractionFailure, name=FAILURE_TOOL, description="The document is not usable."),
        ]
    if mode == "native":
        return NativeOutput([RawInvoice, ExtractionFailure])
    return PromptedOutput([RawInvoice, ExtractionFailure])


def build_extractor(
    settings: Settings,
    *,
    model: Model | str | None = None,
    output_mode: OutputMode = "tool",
    caching: bool = True,
    durable: bool = False,
) -> ExtractorAgent:
    agent: ExtractorAgent = Agent(
        # Haiku, falling back to Sonnet on transient provider errors, inside the shared limiter (M7).
        model or build_model(settings, settings.model_dev, fallback=settings.model_final),
        deps_type=ExtractDeps,
        output_type=_output_type(output_mode),
        name="extractor",
        description="Extracts typed GST invoice data from a PDF, scan or email.",
        instructions=INSTRUCTIONS,
        # No temperature: Haiku 5.5 rejects sampling settings (Pydantic AI drops them with a warning).
        model_settings=ModelSettings(
            max_tokens=settings.max_tokens_default, request_timeout=settings.request_timeout
        ),
        # Two chances to fix *format* problems (bad GSTIN pattern, missing field, wrong buyer).
        retries={"output": 2},
        capabilities=[
            # Each extraction is one-shot, so only the stable prefix (instructions + tool schemas)
            # is worth caching; messages=False skips cache writes on the document itself.
            *([Caching(messages=False)] if caching else []),  # caching=False: M6 experiment only
            # Haiku 5.5 thinks adaptively by default, and the reasoning shares max_tokens with the
            # answer: in the first live run it used ~1-1.4k tokens and truncated 14 answers at 2048.
            # Extraction is copying, not reasoning, so turn it off (the auditor gets it in M6).
            Thinking(effort=False),
            build_cost_hooks(settings),
            build_pii_hooks(),
            # Inside a DBOS workflow, each model request becomes a checkpointed step (M7).
            *([DBOSDurability()] if durable else []),
        ],
    )

    @agent.instructions
    def run_context(ctx: RunContext[ExtractDeps]) -> str:
        # Dynamic instructions, built from deps on every run. Date-only, so the cached prefix
        # stays stable all day.
        return (
            f"Today is {ctx.deps.today:%d %B %Y}. Our company is the buyer; its GSTIN is "
            f"{ctx.deps.company_gstin}. The other party is the supplier."
        )

    @agent.output_validator
    def buyer_is_us(
        ctx: RunContext[ExtractDeps], output: RawInvoice | ExtractionFailure
    ) -> RawInvoice | ExtractionFailure:
        # A format check, so retrying is right: the model most likely swapped supplier and buyer.
        # (Arithmetic is deliberately *not* checked here; see models.py and decisions D4.)
        if isinstance(output, RawInvoice) and output.buyer.gstin != ctx.deps.company_gstin:
            raise ModelRetry(
                f"The buyer GSTIN should be ours, {ctx.deps.company_gstin}, but you returned "
                f"{output.buyer.gstin}. Check whether supplier and buyer are swapped, and re-read it."
            )
        return output

    return agent


def extract_deps(settings: Settings) -> ExtractDeps:
    return ExtractDeps(company_gstin=settings.company_gstin, today=settings.today())
