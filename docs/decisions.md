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
