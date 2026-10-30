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

## D8. Generated data is committed (M2)

`data/` is in git, including the invoices, the ground truth and `ledger.sqlite`. It is
regenerable (the generator is seeded, and a test proves regeneration is byte-identical), but the
developer chose to commit it so a fresh clone runs without a generation step. Approval and
posting state written in later milestones goes to separate files (`app.sqlite`, `dbos.sqlite`),
which should stay out of git.

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

## D13. Auditor and capabilities (M3)

- **`LedgerCapability` is typed against a `HasLedger` protocol** (`.ledger`, `.today`), not
  `AuditDeps`. Deps are contravariant, so the same capability object attaches to the auditor now
  and to Ask-AP in M5. Pyright accepts it with no casts.
- **The auditor adds to the rules; it doesn't repeat them.** Deterministic findings go into the
  prompt as facts, and the model is told not to restate or dispute them. `AuditReport.findings`
  holds only *additional* findings. An output validator stamps them `source="llm"`, so the model
  can't claim a finding is deterministic. `merge_findings` drops any LLM finding of a kind the
  rules already reported, because the rules are authoritative for their kinds. The policy then
  decides on the merged list.
- **Consequence for the M3 metric:** on ground-truth inputs, recall of the *merged* findings is
  mostly the deterministic checks' recall, which is 1.0 by construction. What the live run really
  measures is whether the auditor *adds false positives* (precision), whether it uses the tools
  sensibly, and whether it loads FX only for USD.
- **Thinking is off by default on the auditor too.** Then M6's per-run `Thinking()` escalation
  can be compared against a clean baseline.
- **`FxCapability`** is `Capability[object]` with `defer_loading=True`, and holds one `tool_plain`
  (no deps needed). The model sees a one-line catalog entry and loads it through the built-in
  `load_capability(id="fx")` tool.
- **Retries from tools in V2** (`ModelRetry` from a function tool, and output-tool retries) show
  up as a `ToolReturnPart` with `outcome="retried"` and `retry=RetryInfo(cause="model_retry",
  attempt, max_retries)`. The run scripts read that field.
- **`Caching()` on the auditor** caches messages too (the default), because each tool round
  re-sends the growing conversation.

## D14. The auditor's false positives, and LLM findings capped at warning (M3, after the first live run)

The first full audit run met M3's acceptance (recall 1.00, 4/4 duplicates, FX only on the USD
invoices) but had **precision 0.73** (6 false positives) and 35/39 decisions. The `-v` output
showed the cause: the model used `findings` as a checklist. It wrote 29 "info" findings, most
saying a check *passed*, and some at higher severities. inv-019 was **held** by a critical
`gst_type_mismatch` whose message said "no mismatch exists… recorded only to confirm the check was
made". It also didn't know the stale-date limit (it flagged invoices 5-8 weeks old). Its
narratives reasoned aloud ("above the limit? No, it is below it").

The fixes:
- **A schema field for what the model wants to say:** `AuditReport.checks_passed: list[str]`. With
  nowhere else to report "I checked X, it's fine", the model put it in `findings`. That's a
  general structured-output lesson.
- **Facts computed in code, not left for the model to work out:** the stale-date limit goes into
  the dynamic instructions. For INR invoices, "total ₹X, above/within the limit" goes into the
  prompt. Non-INR totals are deliberately left to the FX tool, so the deferred-capability
  behaviour still has something to test.
- **Instructions:** a finding is a problem, never a passed check. One line on what each kind
  means. "critical" only for a duplicate payment or a blocked vendor. A missing PO goes in the
  narrative. State facts once.
- **Policy (developer's decision):** an LLM-sourced `critical` finding counts as a `warning`.
  Only deterministic rules can hold an invoice; the model can send one to a human, but not
  block a payment. This is the same principle as "the LLM doesn't make the money decision",
  applied to holds.

Result of the rerun (39 invoices, `runs/audit-20261030-024935.json`): precision 0.73 → **0.94**,
decisions 35/39 → **38/39**, auditor advice matching the policy 38/39 → **39/39**, info-finding
noise 29 → 4, cost unchanged ($0.00073 per invoice). The one remaining false positive is inv-027,
the exactly-+5.00% borderline case. The model wrote "about 5.0%, over the 5.0% threshold" and
flagged a warning, which is an arithmetic judgment call at the boundary. That is deliberately left
for M6, where it is the natural test case for per-run `Thinking()` escalation, rather than
patched now.

## D15. Pipeline graph and approvals (M4)

- **The runtime state lives apart from the seed.** `data/ledger.sqlite` (committed) is a
  read-only seed. `open_ledger()` copies it to `data/runtime/ledger.sqlite` on first use, and all
  reads and postings go there. `app.sqlite` sits next to it. `data/runtime/` is git-ignored, and
  `scripts/pipeline.py reset` deletes it.
- **The join uses `reduce_list_extend`, not `reduce_list_append`.** Each check returns a
  `list[Finding]`, so extend flattens them. Parallel checks finish in any order, so the audit step
  sorts the joined findings by kind for stable output.
- **Two kinds of decision.** The first decision routes on the *type* `extract` returned
  (`g.match(ExtractionFailure)` / `g.match(RawInvoice)`). The second routes on a *value*
  (`g.match(Decision, matches=lambda d: d.outcome == "auto_approved")`).
- **There is no `prepare_posting_summary` tool.** The approver already gets the auditor's
  narrative, and the tool would add a model round-trip that repeats it. The poster is two requests:
  call `post_to_ledger` (paused), then return the receipt after approval. As the spec says, the
  poster being an agent at all is mainly so we can learn deferred tools; a plain function would do.
- **One code path for every posting.** The auto-approve step runs the poster until it pauses, just
  as for a human, then answers through the same `resolve()` with `user="policy"`.
- **The paused run is stored as a whole `Conversation`** (`ConversationTypeAdapter`), which holds
  the messages, the usage and the pending `DeferredToolRequests`, along with a `PostingContext`.
  A resumed run in a new process gets `conversation=` plus `deferred_tool_results=`, built with
  `DeferredToolRequests.build_results`, plus fresh deps carrying the approver's identity.
- **Three ways to answer.** `True` approves. `ToolApproved(override_args={...})` approves with an
  edited amount, for a partial posting. `ToolDenied(note)` denies, and the model sees the denial as
  a tool return with `outcome="denied"`.
- **The idempotency key is derived from the invoice content** (vendor, normalized number, date,
  total), not from the upload id. Re-uploading the same invoice maps to the same key, and the
  duplicate check catches it before that point anyway.
- **Double approval has two layers.** The pending row is claimed atomically
  (`pending → resolving`, released if the run fails). The ledger insert is
  `ON CONFLICT(idempotency_key) DO NOTHING`. A resolved approval returns the original receipt
  without a model call.
- **The ledger is the source of truth for receipts**, not the model's `return_receipt` output.
- **Held invoices need a note to approve.** This is enforced in `resolve()`, the service layer that
  both the API (M5) and the CLI call.

## D16. First live pipeline run (M4)

40 invoices, live (`run` without a reset, right after a 3-invoice run): 37/40 decisions, $0.063.
- **inv-001 was held as an `exact_duplicate`, and that was correct.** The earlier 3-invoice run
  had already posted it. The ground truth assumes a fresh ledger, so `run --fresh` now resets
  `data/runtime/` first, and `run` warns when earlier postings exist.
- **inv-027 crashed: the poster refused to post.** The request said "Post invoice
  MDP/0259/26-27" and gave `invoice_ref: "inv-027"`. The model saw two references that didn't
  match and stopped, which was a reasonable instinct given a confusing prompt. The prompt, the
  instructions and the tool docstring now say that `invoice_ref` is our internal document id and is
  *expected* to differ from the vendor's number. Denials now read "An approver (user) denied this
  posting: note", after the inv-029 reply had relayed a denial as a ledger error. (A poster that
  still declines would still crash that invoice's run; failure handling is M7's job.)
- **inv-014 got an auditor false positive:** a `gst_type_mismatch` warning whose message says "No
  mismatch exists… this finding should be disregarded". This is the M3 checklist habit again, now
  rare. The cap stopped it from holding the invoice, but it still routed the invoice to a human.
  This is an LLM finding that *contradicts* a rule which checked that kind and found nothing.
  That is the "deterministic checks disagree" signal the spec uses to trigger per-run `Thinking()`
  in M6, so it is left for M6 rather than patched here.

Confirmation run (`run --fresh`, live): **38/40 decisions, no errors, $0.063** ($0.0016 per invoice
end to end). inv-027 no longer crashes; the poster fix works. The two misses are both the auditor
disagreeing with a rule that found nothing: inv-014 (`gst_type_mismatch` on a correct inter-state
IGST invoice, the same as the first run, so it's reproducible) and inv-027 (`price_drift` at
exactly +5.00%, as in M3). Both go to M6 as the test cases for "disagreement → re-run with
`Thinking()`".

## D17. API, streaming, CLI and Ask-AP (M5)

**Streaming the extraction.**
- **`run_stream()` can't retry.** A failed output validation raises *"retries are not supported in
  `run_stream()` requests"*. The extractor relies on retries (the buyer GSTIN, schema errors), so
  the extract step keeps `run()` and observes the stream with `event_stream_handler`. A retry then
  shows up as a new attempt whose partial JSON starts over.
- **Partial JSON, not partial objects.** `stream_output()` validates every snapshot, and a
  `RawInvoice` only validates once its last required fields (`subtotal`, `total`, `confidence`)
  arrive, so it yielded about 2 objects per invoice, both at the very end. SSE instead publishes
  the output tool's raw argument text parsed with `pydantic_core.from_json(allow_partial=True)`
  (unvalidated, growing). The validated object is the step's result.
- **Graph step events come from `graph.iter()`.** It yields each batch of tasks before they run,
  each with a `node_id`, so no step needed changing. The join appears once per arriving branch
  (four times).
- **Events go through an in-process `EventBus`** with replay, so a late subscriber sees the whole
  run. A single process only; a multi-worker deployment would need Redis or similar.

**Ask-AP.**
- **The memory is a whole `Conversation`** (messages, usage, id) in `app.sqlite`. Because it
  carries usage, `UsageLimits(request_limit=30)` is a budget for the conversation; a test proves
  turn 3 is refused at a limit of 2.
- **The `session` instructions are `on_change="append"`.** When the user changes mid-conversation,
  an `InstructionDeltaPart` is appended to the history and the prefix stays the same, so the
  cached prefix survives.
- **It reuses `ledger_capability` as is** (`AskDeps` satisfies `HasLedger`), plus local
  `explain_invoice`, `list_invoices`, `find_vendors` and `vendor_spend`. `vendor_spend` does the sum
  in code. M6 moves the ledger side to the MCP server.

**The API (FastAPI).**
- **Two DI systems.** FastAPI `Depends` injects the app-lifetime `Services` built in the lifespan.
  Routes build the per-run agent deps (who is asking, today) from them. Tests inject `Services`
  directly, with FunctionModel agents and no lifespan.
- **`POST /invoices` takes a multipart `file`** or JSON `{"path"}`. Paths must resolve inside
  `data/`, so the endpoint can't read arbitrary files. Uploads go to `data/runtime/uploads/`. The
  pipeline runs as a background task; a failure is recorded on the invoice as `status="failed"`
  with the error, and published to the stream.
- **Error mapping.** `X-User` is required for approvals. An `ApprovalError` maps to 404 (unknown)
  or 409 (refused). `UsageLimitExceeded` maps to 429.
- **`/batches` returns 501** until M7 (DBOS).
- **No FastAPI tracing:** `logfire.instrument_fastapi` needs `opentelemetry-instrumentation-fastapi`,
  which isn't on the §19.2 list. The agent and graph spans already cover the work.
- **A gotcha:** `request.form()` returns *Starlette's* `UploadFile`; FastAPI's is a subclass, so
  `isinstance` must check the Starlette class.

**The CLI.** `invoiceiq` (typer) replaces `scripts/pipeline.py`. It has `serve`, `ingest`, `run`,
`watch`, `pending`, `approve`, `deny`, `chat`, `postings`, `costs` and `reset`, all in-process
through the same service functions as the API. `watch` exists because `curl` isn't installed on
the dev machine; it also makes the growing JSON readable.

## D18. The ledger as an MCP server, and Ask-AP as its client (M6)

- **`FastMCP` is now `MCPServer`.** In mcp 2.x, `FastMCP` was renamed (`from mcp.server.mcpserver
  import MCPServer`), and the old import path raises with a pointer to the migration guide. The
  decorators (`@server.tool()`, `@server.resource(...)`) and `run(transport="stdio" |
  "streamable-http")` are unchanged in spirit.
- **The tools are `find_vendor`, `get_purchase_order`, `vendor_spend` and
  `list_flagged_invoices`**, plus a `ledger://policy` resource rendered from the live `Policy`.
  The spec's `vendor_spend(vendor_id, from, to)` uses `start`/`end`, because `from` is a Python
  keyword. All of them are read-only.
- **Ask-AP's default is `ledger="mcp"`:** `MCP(url=MCP_URL, id="ledger", tool_prefix=True)`, or
  with no URL, `MCP(local=build_mcp_server(settings), ...)`. That is the same server connected
  in-process: still the MCP protocol, without a second terminal. `tool_prefix=True` with
  `id="ledger"` names the tools `ledger_*`, so they can't collide with Ask-AP's local
  `explain_invoice` and `list_invoices`. `ledger="local"` keeps the M5 direct tools as an offline
  fallback.
- **All three transports were checked:** in-memory (tests), streamable HTTP
  (`invoiceiq mcp --http`), and stdio (the server launched as a subprocess, as Claude Code does).
- **Three ways to use an MCP server from Pydantic AI:**
  - **`MCP(...)` capability** (used here): the recommended entry point. It runs the client
    locally, so tool calls go through our hooks, cost ledger and tracing. It takes a URL, a
    client or an in-process server, and handles prefixing and `allowed_tools` filtering.
  - **`MCPToolset(...)` in `toolsets=[...]`:** the lower-level object the capability wraps. It's
    useful for direct calls (`direct_call_tool`, `read_resource`), which is how the tests use it.
  - **`MCP(url=..., native=True)`:** the *model provider* (Anthropic) connects to the server
    itself. That needs a publicly reachable URL, and the calls bypass our process (no local hooks
    or spans for them), so it's documented here rather than run against a localhost server.
- **Distributed tracing is declared on purpose** (`distributed_tracing=True`). The in-process MCP
  client propagates trace context so the server's spans nest under the agent run, and Logfire
  warns unless that's declared intentional.

## D19. Per-run Thinking escalation for the auditor (M6)

The two remaining live misses (inv-014, inv-027) were both the auditor *disagreeing with a rule*:
a non-info finding of a kind the deterministic checks had checked and found clean. Every finding
kind has a rule, so that's easy to detect (`disagreements()`). `run_audit()` audits once with
thinking off. If the result disagrees with the rules, or the extraction confidence is below 0.8
(the spec's rule), it audits **once more** with `capabilities=[Thinking(effort="medium")]` for that
run only, and `max_tokens=8192`, because thinking shares the budget (D11). The second, independent
report replaces the first. Only invoices that would otherwise change outcome pay for reasoning.
A per-run capability is merged over the agent's own `Thinking(effort=False)` and wins. The
setting is `AUDIT_ESCALATION` (default on), and the record keeps `audit_escalation` (the reason).
`scripts/exp_thinking.py` measures off vs escalation vs always-on on the hard cases.

## D20. M6 experiment results, and two fixes they exposed

**Caching** (10 invoices each, `runs/exp-caching-*`): the extractor saved **17%** (42% of input
read from the cache; one-shot requests can only reuse the ~2.2k-token instructions+tools prefix).
The auditor saved **46%** (77% read from the cache: every tool round re-sends the growing
conversation, so `Caching()` with messages pays off). Cache writes (1.25×) were outweighed in both.

**Thinking** (4 hard cases × 3 repeats, `runs/exp-thinking-*`): off made 3 false positives,
9/12 decisions and $0.00056 per audit. **Escalation made 0 false positives, 12/12, $0.00083** (it
thought on 3/12 audits, 236 reasoning tokens per audit on average). Always-on made 0, 12/12 and
$0.00097 (695 reasoning tokens). Escalation matches always-on at about a third of the reasoning
cost, so it stays on. Every escalation was inv-027: thinking turned "about 5.0%, flag it" into
"exactly 5.00%, at the threshold, not flagged", which is the right call. inv-014 didn't
false-alarm in any mode in this run (it's intermittent), so its escalation is proven only by a
unit test so far. The full audit run with escalation: precision 16/17, decisions 38/39, 1
escalation.

**Fix 1: thinking silently dropped the auditor's evidence.** With thinking on, Pydantic AI
switches Anthropic to strict native output (a tool call can't be forced while thinking), and
strict mode can't express `dict[str, str]`, so it forces `{}`. Every thinking run's findings had
`evidence={}`, and every non-thinking run's had data. The auditor now outputs
`AuditFinding(kind, severity, message, evidence: list[Evidence(name, value)])`, with no `source`
field (the schema, not a validator, stops the model from claiming "deterministic"), and
`merge_findings` converts it to `Finding(source="llm")`. A test asserts the `AuditReport` schema
has no `additionalProperties` (no dicts). Older stored records with dict evidence still load.

**Fix 2: evaluations used the runtime ledger** (my regression from M4). After the pipeline had
posted inv-034, re-auditing it found an exact duplicate (correct for the runtime, wrong for an
eval). `audit_deps(seed=True)` now gives `run_audit.py`, the experiments and `check_invoice.py`
the committed seed ledger.

**Confirmation runs** (after both fixes): escalated inv-027 findings now keep their evidence
(`invoice_unit_price`, `po_unit_price`, `drift_percent`). The full audit run reached **39/39
decisions, precision 16/16, no false positives**, at $0.00077 per invoice. Escalation fired 3
times: inv-027 (price_drift → info) and, for the first time live, two of the inv-014 kind, a
`gst_type_mismatch` false alarm on correct inter-state IGST (inv-019, inv-029). Each was
withdrawn after thinking. **MCP acceptance:** Ask-AP answered "what did we pay Shri Balaji
Engineering last quarter?" with INR 36,981.17 over 2 invoices. That matches the ledger (P-00029 +
P-00030, Aug 2026), reached through `ledger_find_vendor` → `ledger_vendor_spend` over streamable
HTTP, with `MCP send tools/call` spans visible in the trace.

## D21. Reliability and durable batches (M7)

**Reliability (`reliability.py`), all native mechanisms:**
- **`FallbackModel(haiku, sonnet, fallback_on=TRANSIENT)`** for the extractor. TRANSIENT is rate
  limit, unavailable/overloaded, server error, connection and request timeout. The default
  (`ModelAPIError`) would also fall back on an auth error or an oversized prompt, where Sonnet
  fails the same way and costs more. A test proves an auth error does *not* fall back.
  `FallbackModel` is request-level: it can't fall back when *output validation* is exhausted
  (`UnexpectedModelBehavior`). That case is recorded as `invalid_output` for a human to look at.
- **A shared `ConcurrencyLimiter`** (`MODEL_CONCURRENCY`, default 4) wraps every agent's model in
  `ConcurrencyLimitedModel`, so at most N requests are in flight per process, whatever runs them.
- **Two timeouts from settings.** `ModelSettings['request_timeout']` (60 s) bounds one request,
  including SDK retries. `run(timeout=...)` (180 s) bounds one agent run and raises `RunTimedOut`.
  429s are first retried with backoff by the Anthropic SDK itself.
- **Error categories.** `error_category()` maps the V2 exception hierarchy (`ModelAPIError` and
  its subclasses, `UsageLimitExceeded`, `RunTimedOut`, `UnexpectedModelBehavior`,
  `FallbackExceptionGroup`, `BudgetExceeded`) to stable labels. `process_invoice()` turns any
  failure into a stored `failed` record with `"<category>: <error>"`. The API, the CLI and the batch
  all use it, so a failed invoice says why it failed and the rest of a batch carries on.
- **Every agent run carries `metadata={"invoice_id": ...}`**, and `CostHooks` writes it into each
  cost-ledger line. That gives cost per invoice, and the live crash test's re-spend check.

**The durable batch (`pipeline/batch.py`), DBOS 3.4 on SQLite:**
- **Each invoice is a child workflow** (`{batch_id}:{invoice_id}`), enqueued in a fixed order on a
  DBOS queue with `worker_concurrency=4`. The batch workflow waits for them in order. A recovered
  batch finds the same children and their recorded results.
- **`DBOSDurability()` on the extractor and auditor:** each model request is a checkpointed
  step, and the agent names ("extractor", "auditor") must never change. The poster is not durable;
  its runs happen inside our `post`/`park` steps instead. `DBOSDurability` over
  `ConcurrencyLimitedModel(FallbackModel(...))` was checked: the fallback works inside the step,
  and a replay makes zero model calls.
- **Our own I/O goes through `deps.run_step(name, fn)`:** a direct call normally, and
  `DBOS.run_step_async` in a batch. That covers the three ledger checks, `post`, `park`,
  `save_rejected` and `record_failure`. The duplicate check *must* be a step: after an invoice is
  posted, a replay would see its own posting and call it a duplicate.
- **The `post` step is safe to re-run.** An interrupted step re-executes:
  `request_posting` re-parks (resetting a stale `resolving` claim), and `post_to_ledger`'s
  idempotency key returns the first posting.
- **dbos 3.x API changes** from what the spec assumed: queues come from
  `DBOS.register_queue[_async]` (persisted in the system database, so DBOS must be launched first),
  not `Queue(...)`. The batch workflow registers the queue on first use, idempotently, so a
  recovered batch can't race the registration. `application_version` is fixed in settings,
  because DBOS only recovers workflows recorded under the same version.
- **WAL mode on the runtime `ledger.sqlite` and `app.sqlite`** (never the seed): concurrent batch
  workers no longer queue on SQLite locks. DBOS's own checkpointing on SQLite still adds 1-3 s per
  invoice under concurrency. DBOS itself says SQLite is for development; production would use
  Postgres.
- **The in-process batch graph** (`run_batch_in_process`) uses the spec's `.map()`: one branch per
  invoice, joined with `reduce_list_append`. It's fast, but not crash-safe; it backs
  `invoiceiq run --parallel`.

**Crash test** (`scripts/crash_test.py`, and `pytest -m slow`): fake mode, 40 invoices, `kill -9`
after 15. Result: 40/40 finished, 40/40 decisions right, 20 postings for 20 posted invoices, 0
duplicate keys, and 80 model calls (40 extract + 39 audit + exactly 1 repeat, for the invoice in
flight at the kill). Two harness lessons: kill the worker itself, not a `uv run` wrapper (its child
survives); and `pkill -f` can match the shell running it.

**Durable execution vs Conversation storage (§11.4).** DBOS keeps one *run* alive through a
crash. A stored `Conversation` keeps a *thread* alive across requests (Ask-AP, approvals). Neither
replaces the other, and DBOS isn't used for chat history.

## D22 — M7 live crash test: two bugs the fakes couldn't show (2026-10-30)

The first live `crash_test.py --live` failed: 3 invoices `failed`, plus a reported re-spend on inv-016.

1. **Per-run `Thinking` is rejected inside a DBOS workflow.** `UserError: Capabilities added per-run
   inside a DBOS workflow are not supported`. DBOSDurability registers durable steps when a capability
   is bound to the agent, before the workflow starts, so M6's `run(capabilities=[Thinking(...)])` escalation
   can't run durably. The fake auditor never escalated, so the $0 tests passed.
   *Fix:* `build_auditor(..., thinking="medium")` builds a second agent, `auditor_thinking` (a distinct
   name, because DBOS step names derive from it), with thinking and `ESCALATION_MAX_TOKENS` fixed at
   construction. `start_runtime` passes it as `PipelineDeps.escalation_auditor`, and
   `run_audit(escalation_agent=)` uses it. In-process runs keep the per-run capability.
   *Test:* the fake batch auditor now raises a false alarm on inv-014 that its thinking twin withdraws,
   so every fake batch, and the fake crash test, exercises a durable escalation.
2. **"Re-spend" was mostly double counting.** After the kill, inv-016's extractor and auditor cost lines
   were byte-identical to the originals: DBOS replayed the checkpointed responses, but the
   `after_model_request` hook runs outside the durable step and recorded them again.
   *Fix:* `record_response` is idempotent per `provider_response_id`, which a replayed response keeps.
   The only real repeat was the poster, and that was legitimate: `auto_post` saves the `posted` record
   *inside* its step, the kill landed before the step's checkpoint, and so the step re-ran (and
   `post_to_ledger`'s idempotency key returned the original posting).
   *Fix to the test:* "finished before the kill" now means the child workflow is `SUCCESS` in DBOS's
   `workflow_status`, not "has a terminal record".

Lesson: a fake that never takes a branch doesn't test that branch durably. Every behavior that changes
how an agent is *called* (per-run capabilities, settings, toolsets) needs one fake path in the crash test.

## D23 — M8: evals, online evaluation and the Harness (2026-10-30)

**Added `pydantic-ai-harness==0.63.0`** (spec §19.2 allows it in M8). Pinned exactly because it's 0.x;
it pins `pydantic-ai-slim==2.63.0` itself, so the two move together.

**Evals (`evals/`).** There are three `pydantic_evals` datasets.
- **Custom case evaluators:** FieldAccuracy (±₹1), LineItemF1, KindCorrect, FindingRecall,
  FindingPrecision and DecisionMatch.
- **Built-in evaluators:** MaxModelRequests, ToolCorrectness, TrajectoryMatch, MaxToolCalls and LLMJudge.
- **Headline metrics** come from `ReportEvaluator`s as `ScalarResult`s, micro-averaged over all
  findings or fields. `evals.compare` gates on them against `evals/baseline.json`; a drop of more than
  3 points, or any new task failure, fails the gate.
- **Span capture is enforced, not assumed.** A `SpansCaptured` evaluator runs on every case, and the
  run exits non-zero if any case has no model-request spans. Without that, the agentic evaluators
  score 0 silently.

**Changes from the spec:**
1. ToolCorrectness and TrajectoryMatch are attached per case, because they only make sense where the
   right investigation is known. TrajectoryMatch expects `get_vendor → search_posted` in order,
   which is what the live M6 trails show the auditor doing.
2. The audit eval scores the decision path: rules, then the auditor with escalation, then the merge,
   then the policy. The auditor alone isn't scored, because the rules hand it their findings as facts.
3. Each dataset's expected output uses the task's output type (`AuditResult` with only `flagged` and
   `decision` filled), because `Dataset[I, O, M]` types `expected_output` as `O`.

**OnlineEvaluation on the auditor.** It samples 20% of runs (`ONLINE_EVAL_RATE`) and runs
MaxToolCalls(8) and NarrativeNotEmpty. Results arrive as `gen_ai.evaluation.result` OTel log
events; `tests/test_online_eval.py` captures them with `capfire`.
- Tests use a rate of 0, so no background evaluations run at random.
- It also works inside the DBOS batch. The fake crash test passes with it on.

**ApprovalReviewer and ToolCallJudge on the poster.** They give a second, cheap model opposite
powers.
- **The reviewer only approves.** Its selector limits it to `awaiting_approval` invoices with no
  findings: the high-value-but-clean tier. It sees the conversation, because the call's arguments
  don't name the vendor. When it approves, the call runs in the same run, `request_posting` returns
  the receipt, and the ledger's `posted_by` is `approval-reviewer`.
  *It protects against approval fatigue*, where people rubber-stamp routine high-value postings.
- **The judge only blocks, and only overposting.** You chose this, so partial postings still work.
  It runs at execution time, after any approval, on the edited arguments. It fails *open*
  (`on_uncertain="allow"`), because `post_to_ledger` already refuses `amount > total` in code, which
  is the hard boundary; failing closed would turn a judge outage into denied postings. A blocked
  call comes back as an ordinary tool return carrying `metadata["tool_call_judge"]`, not as a
  `denied` outcome.
  *It protects against a model, or tampered history, changing the amount* between the request and
  the posting.
- **Their own model requests are invisible to the host agent's capabilities.** Their tokens join the
  run's usage, but neither our `CostHooks` nor `SpendLimits` sees them, because each Harness
  capability builds its own internal agent. `CostRecordingModel`, a `WrapperModel`, records them;
  it's the only part of those agents we choose. The same applies to `LLMJudge`. Every guard model
  sets `max_tokens=256` and `thinking=False`.
- `POST_LIMITS.request_limit` goes from 4 to 6, because the guards claim requests from the same run
  usage.

**SpendLimits vs CostHooks.** `tests/test_spend_limits.py` compares them; CostHooks stays the
project's meter.
- **Same dollars.** Both price with genai-prices and record the same totals.
- **SpendLimits is a brake.** It checks before every *request*, so it stops a run partway through.
  CostHooks checks the project total before each *run* only.
- **SpendLimits' accrual is a durable operation.** A DBOS replay counts each response once on its
  own. CostHooks needed D22's `provider_response_id` deduplication to do the same.
- **Where its counters live.** SpendLimits keeps them in a store: in-memory by default, or Redis or
  Postgres to share across processes. This project shares one SQLite or file ledger across the CLI,
  API and batch workers, which neither shipped store covers.
- **Verdict:** keep CostHooks as the ledger and cap. Reach for SpendLimits when you need per-tenant,
  per-day or per-run brakes, with Redis or Postgres underneath.

**StepPersistence: not adopted.** DBOS already checkpoints every step, and we persist the poster's
pause as a `Conversation`. StepPersistence saves message-history snapshots at settled steps. That
would duplicate DBOS for crash recovery and the `Conversation` for pauses, so there's no gap for it
to fill here.

**Live results (2026-10-30).** The baseline was accepted for Haiku.

| Dataset | Haiku | Sonnet | Cost (Haiku / Sonnet) |
|---|---|---|---|
| extraction (40) | field 1.00, kind 1.00 | field 1.00, kind 1.00 | $0.028 / $0.588 (21×) |
| audit (39) | recall 1.00, precision 1.00, decisions 1.00 | identical | $0.030 / $0.686 (23×) |
| narrative (10, Haiku judge) | judge pass 0.80, ≤4 sentences 1.00 | — | $0.0075 |

- **Haiku vs Sonnet:** on this corpus Sonnet buys nothing for about 22× the cost. Haiku stays the default.
- **Lesson: `TrajectoryMatch(order="in_order")` is an F1 score, not an ordering check.** It takes the
  longest common subsequence, and every extra tool call lowers precision.
  - `[get_vendor, get_po, search_posted]` against `[get_vendor, search_posted]` scores 0.8.
  - A thorough 8-call investigation scores 0.4, even though the auditor did the right thing.
  - It's diagnostic only and not gated. To ask "did X happen before Y?", match exactly the tools
    you expect (`order="exact"`) or write a subsequence evaluator.
- **Open:** the eval report's `requests` and token metrics are double the cost ledger's for live
  Anthropic runs: 2 `chat` spans per real request. Its `cost` metric matches the ledger, and
  `FunctionModel` runs show 1 span, so the second span comes from the live client path. The headline
  metrics are computed from outputs and aren't affected. Check it in M9 with one Logfire trace.

## D24 — M9: polish and the fresh-clone check (2026-10-30)

**The fresh-clone check caught one real bug.** The check: copy only what git ships into a scratch
directory, then `uv sync && gen_mockdata && pytest && invoiceiq serve`, with no `.env` and no key.
- The first three steps passed (194 tests).
- `serve` crashed on startup with a 30-line traceback. The agents are built eagerly, and the Anthropic
  provider refuses to construct without `ANTHROPIC_API_KEY` (spec §2.1's first gotcha).
- **Fix:** every command that builds agents (`serve` and everything through `_setup`) now checks for
  the key first. Without one, it prints `cp .env.example .env` and exits 2.
- `tests/test_cli.py` covers it. The test replaces the CLI's settings loader rather than changing
  directory, because `Settings` reads `.env` by absolute path. An early version of the test picked up
  the real key and started a real server. No money was spent: model requests are blocked in tests.
- With a key, `serve` starts and `/healthz` answers in the fresh copy.

**Results table:** `scripts/benchmark.py` runs the 40 invoices one at a time (so each latency is the
invoice's own), and reports decisions, cost per invoice (from the ledger's invoice ids, with the
guards' unattributed lines spread evenly) and p50/p95 latency.

**Docs:**
- README: quick start, architecture diagram, results, demo, layout.
- `docs/observability.md`: what to capture for spec §15, plus M8's double-span open item.
- `notes/framework-comparison.md`: the template for the developer's column.
