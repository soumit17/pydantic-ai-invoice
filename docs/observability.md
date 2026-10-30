# Observability

Logfire is plain OpenTelemetry. With no `LOGFIRE_TOKEN` nothing leaves the machine; spans still exist in-process, which is what the evals' span-based evaluators read.

**See spans:**
- **In the terminal:** add `--console` to any CLI command, e.g. `uv run invoiceiq run inv-024 --console`.
- **In the Logfire UI:** put `LOGFIRE_TOKEN` in `.env` (the free personal plan is enough).

Setup is in [src/invoiceiq/telemetry.py](../src/invoiceiq/telemetry.py):
- `logfire.instrument_pydantic_ai(include_binary_content=False)` keeps base64 PDFs out of traces.
- `scrubbing` masks bank and phone numbers.
- `distributed_tracing=True` lets the MCP server's spans nest under Ask-AP's run.

## What to capture (spec §15)

Add a screenshot and a sentence under each item.

1. **One full pipeline trace.** Graph steps → agent runs → model requests → tool calls. Try `uv run invoiceiq run inv-024 --console`.
2. **Usage on run spans:** `gen_ai.aggregated_usage.*` on each `invoke_agent` span (instrumentation v5).
3. **Prompt-cache health:** `pydantic_ai.cache.*` attributes (hit ratio, collapse events). Run the auditor twice in a row.
4. **A failed-then-retried output validation.** The extractor's validator raises `ModelRetry`, and the retry shows as a tool return with `outcome="retried"`. The M2 runs logged several; `scripts/run_extraction.py -v` prints the reasons.
5. **The FX capability loaded only for USD invoices.** On inv-038, look for `load_capability(fx)`, then `convert_to_inr`; on an INR invoice, neither appears.
6. **M8 additions:**
   - `gen_ai.evaluation.result` events from `OnlineEvaluation` (set `ONLINE_EVAL_RATE=1` to see one every run).
   - `review tool approval` spans (ApprovalReviewer) and `judge tool call` spans (ToolCallJudge) on the poster.

## Open item from M8

The eval reports show **two `chat` spans per live Anthropic request**, so their `requests` and token metrics are double the cost ledger's. The `cost` metric is right, and fake models show one span. Find both spans in one trace and note their names and parent spans here:

```bash
uv run python scripts/run_extraction.py --ids inv-001 --console    # ~$0.001
```
