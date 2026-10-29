# Decisions

ADR-style notes, one per design trade-off. Newest at the bottom.

## D0. Pinned versions (M0, 2026-10-30)

Python 3.12.15 (via uv). The pins are exact in `pyproject.toml`, and `uv.lock` pins the rest.

| Package | Version |
|---|---|
| pydantic-ai-slim[anthropic,mcp,evals,logfire] | 2.63.0 |
| pydantic-graph | 2.63.0 |
| pydantic-evals | 2.63.0 |
| pydantic | 2.14.2 |
| anthropic | 2.3.0 |
| logfire | 5.4.0 |
| genai-prices | 0.2.0 |
| mcp | 2.3.0 |
| fastapi / uvicorn / sse-starlette | 0.162.0 / 0.54.0 / 3.6.0 |
| httpx / typer / pydantic-settings / aiosqlite | 0.28.1 / 0.27.3 / 2.16.2 / 0.22.1 |
| reportlab / pypdfium2 / pillow / faker | 5.1.0 / 5.14.0 / 13.0.0 / 40.44.0 |
| pytest / pytest-asyncio / pytest-cov / pyright / ruff | 9.2.0 / 1.4.1 / 7.1.0 / 1.1.414 / 0.17.3 |

Not installed yet: `pydantic-ai-harness` (comes in M8, pinned exactly) and `dbos` (comes in M7).

## D1. API differences from the spec (verified against the installed 2.63.0 source)

The spec was written against ~2.59. Every name it uses for M0–M2 exists. The differences:

- **Hook names.** On `Hooks(...)` and `@hooks.on.<name>`, the hook names don't have the
  `on_`/`wrap_` prefixes: `before_run`, `after_model_request`, `model_request_error`,
  `tool_execute` (wrap), and so on. The prefixed names (`on_model_request_error`, `wrap_tool_execute`)
  are method names on `AbstractCapability`, which you only use when subclassing. The spec's
  `wrap_tool_call` is now called **`tool_execute`** / `wrap_tool_execute`.
- **`output_retries` is no longer an `Agent(...)` argument.** Use `retries={'output': N}` (an
  `AgentRetries` dict), or `ToolOutput(max_retries=...)` for each output tool.
- **Caching(messages=False)** exists exactly as in the spec. It caches only the tools and the
  static instructions.
- **The Anthropic model is now a subpackage** (`pydantic_ai/models/anthropic/`). Only the import
  path changed; the `'anthropic:...'` model strings are unchanged.
- **Model IDs** `claude-haiku-5-5` and `claude-sonnet-5-5` are both known to pydantic-ai and to
  genai-prices.
- **Pricing is tiered.** In genai-prices, Haiku 5.5 costs $0.10/M input and $0.50/M output, but
  *any request whose prompt goes over 100k tokens bills every token at 5×*. Keep prompts well under
  100k. That matters for the multimodal PDF invoices.

## D2. Cost ledger lives in this package

The spec refers to `shared/cost.py` from the master plan, which isn't in this workspace. It lives
at `src/invoiceiq/shared/cost.py` and writes to `runs/costs.jsonl`. Prices come from
`ModelResponse.cost()` (genai-prices) rather than a hand-kept rate table. `CostHooks` (M2) will
call the same `record_response()` from an `after_model_request` hook.

## D3. Agents are built by factories

Constructing an `'anthropic:'` agent fails if `ANTHROPIC_API_KEY` is unset (spec §2.1). Agents are
built by `build_*_agent(settings)` functions instead of at module import, so importing never needs
a key. The test `conftest.py` also sets a dummy key and turns off `ALLOW_MODEL_REQUESTS`.

## D4. Two invoice models: lenient `RawInvoice`, strict `Invoice` (M1)

If the extractor's output type validated arithmetic, a validation retry would push the model to
"fix" a vendor's real mistake, and category-10 anomalies would vanish. So `RawInvoice` checks only
the format (GSTIN pattern, money precision, at least one line). The arithmetic and GST-type rules
live as plain functions in `gst.py`. Two things use those functions: `Invoice` (a strict subclass
whose validator raises) and `checks.strict_validate` (which turns each failure into a finding).
There is one source of truth, and two ways to apply it.

## D5. Finding conventions (M1)

- There is at most one finding per kind per invoice; the evidence lists every affected line. That
  lets evals compare sets of `(kind, severity)`.
- Severities: duplicates (exact and fuzzy), blocked vendors and GST-type mismatches are
  **critical**, per §4.2. Everything else is a **warning**. A vendor with KYC pending reuses
  `blocked_vendor` at warning severity. `info` findings never change the decision.
- Price drift is **strictly greater than** 5%, and only upward; a lower price is not a problem
  for AP. The borderline invoice (exactly +5.00%) therefore has no deterministic finding and
  auto-approves. The auditor may still comment on it.
- Foreign-currency invoices always get an `fx_unverified` warning, so they always go to a human.
  Their INR value uses the static mock table `policy.FX_RATES_INR` (USD 84, EUR 91).
- Fuzzy duplicate: same vendor, same date, same total, and the same invoice number after dropping
  everything except letters and digits (`INV/26/042` ≈ `INV-26-042`).
- PO lines are matched to invoice lines by HSN/SAC code, falling back to line position.

## D6. Reproducible "today" (M1)

The mock data is dated relative to 2026-10-29. `AS_OF_DATE` in `.env` pins "today" for the date
checks and the evals; leave it unset to use the real date.

## D7. Mock data choices (M1)

- The Hindi/English vendor names are written in Latin script ("Shri Balaji Engineering Works",
  "Maa Durga Packaging Udyog"). Without `uharfbuzz` (not on the §19.2 list), reportlab can't shape
  Devanagari, so it would render wrongly. Real Devanagari is a possible stretch goal.
- The PDFs use `Canvas(invariant=1)`, so regeneration is byte-identical for every file, not only
  the ground truth. A test asserts this.
- Expected findings and decisions are written by hand for each category in the generator, never
  computed by the checks, so the M1 acceptance test really tests the checks.
- `repos/app_store.py` (invoices, conversations, pending approvals) is deferred to M4, where the
  approval flow decides its schema.

## D8. Generated data is not committed (M2)

All of `data/` is git-ignored, including `ground_truth/`. The spec suggested committing the ground
truth, but the generator is seeded and a test proves regeneration is byte-identical, so the script
is the source of truth. A fresh clone runs `uv run python scripts/gen_mockdata.py` first.

## D9. Extractor wiring (M2)

- **Output tools have explicit names** (`ToolOutput(..., name="invoice")` and
  `name="extraction_failure"`), so traces and tests read clearly. The model chooses the kind by
  which tool it calls, and text output is disabled.
- **`retries={'output': 2}`** replaces the spec's `output_retries=2` (see D1).
- **The buyer-GSTIN validator** raises `ModelRetry` when the buyer isn't us. That is a format or
  orientation error, which a retry can fix. After 2 retries the run raises
  `UnexpectedModelBehavior`, and M7 decides what to do with it.
- **In V2, a retry on an output tool** goes back to the model as that tool's `ToolReturnPart`,
  carrying the `ModelRetry` message, not as a separate `RetryPromptPart`. Look for that in traces.
- **PII has two layers.** `PIIRedactionHooks` writes a redacted audit log (logger
  `invoiceiq.audit`) and returns every request and response unchanged. Logfire's native
  `ScrubbingOptions` uses the same regex in a callback, so it masks only the numbers inside span
  attributes. Logfire combines all scrubbing patterns into one regex, so the patterns must not use
  inline flags like `(?i)`.
- **No binary content in traces.** `instrument_pydantic_ai(include_binary_content=False)` keeps
  base64 PDFs and scans out of the spans.
- **`UsageLimits(cost_limit=...)` exists in 2.63** as a native cost cap for each run. The
  project-wide cumulative cap still needs the `before_run` hook, because UsageLimits only sees one
  run.
- **Live runs go one at a time** in `scripts/run_extraction.py`. Concurrency limiting arrives in
  M7 with the native mechanism (Rule 0.3).

## D10. Validation-as-retry vs findings (the §7.1 trade-off)

Validation-as-retry is the right tool when the *model* is wrong in a way the document can
correct: a malformed GSTIN, a missing field, swapped parties. The validator's message tells the
model what to re-read, and the second attempt usually succeeds.

It is the wrong tool when the *document* is wrong: a vendor's bad multiplication, IGST on an
intra-state supply. A strict validator would reject the faithful extraction and ask the model to
"try again" until it produced numbers that pass, which means inventing them. The run would look
healthy, the invoice would look clean, and the anomaly the audit exists to catch would be gone.
In this domain, that's the worst kind of failure: confidently wrong, and invisible.

So the extractor's output type validates only format (`RawInvoice`), and content rules run
afterwards as deterministic checks that produce findings (`strict_validate`). Rule of thumb: if
a human re-reading the document would *fix* the value, retry. If they would *report* it, make it
a finding.

## D11. Thinking off for the extractor (M2, after the first live run)

The first live run (40 invoices, tool output) had 10 retries and 3 `UsageLimitExceeded` errors.
The cost ledger showed 14 requests ending at *exactly* `max_tokens=2048`. A 4-8 line invoice
needs only about 400-650 tokens of JSON, but requests used 1,400-2,048. Haiku 5.5 uses adaptive
thinking by default, and the reasoning shares `max_tokens` with the answer, so longer invoices were
cut off mid-answer and retried. Extraction is copying, not reasoning, so the extractor now has
`Thinking(effort=False)`. The developer chose this over raising `max_tokens` to keep thinking.

Related findings from the same run:
- Haiku 5.5 rejects sampling settings, so `temperature` is gone. Pydantic AI had been dropping it
  with a warning.
- `AgentInfo.model_settings` in a `FunctionModel` shows only the agent's own settings. Settings
  that capabilities add (thinking, cache) are merged later and reach `Model.request`, which is
  where the test checks them.
- `Caching(messages=False)` works: 99k tokens were read from the cache across the run.
- Two instruction refinements: descriptions leave out the codes printed beside them ("(HSN
  4819)"), and `total_tax` may be summed from the printed CGST/SGST/IGST totals when the layout
  doesn't print it as one figure. That's the single exception to "never compute".
- The cost ledger now records `reasoning` tokens, and `run_extraction.py -v` prints the message
  behind every retry.

## D12. Output mode stays `tool` (M2 experiment)

On 10 invoices after the thinking fix, all three modes were 10/10 and 100% on fields. Prompted
output was cheapest ($0.0058 against $0.0084 for tool output) and had no retries, but n=10 with
one run each can't separate the modes on accuracy. The cost gap is about $0.0003 per invoice. Tool
output stays the default for these reasons:
- **The union decision is visible.** The model picks `submit_invoice` or `report_failure`, which
  reads clearly in traces and tests.
- **The schema is sent as a tool definition**, and partial tool arguments are what M5 streams.
- **Prompted output only validates after generation.** Nothing at the API level holds the model
  to the schema.

The two retries in tool mode were the model calling an unknown tool `invoke`. The output tools are
now named `submit_invoice` and `report_failure`. The `gst_rate` instruction now separates a
printed 0% (`0`) from no rate printed at all (`null`), after inv-028's 0% line came back as null.
