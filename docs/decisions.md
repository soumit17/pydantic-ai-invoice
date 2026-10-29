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
