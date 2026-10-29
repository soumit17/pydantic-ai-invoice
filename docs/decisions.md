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
