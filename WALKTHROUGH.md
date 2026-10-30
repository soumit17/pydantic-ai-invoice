# InvoiceIQ: a walkthrough of every scenario that was run

This branch (`runs-and-logs`) is `main` plus everything that running the project produced: the cost ledger, every script's results, the crash tests' worker logs and databases, the eval reports, and the final pipeline state.

This document walks through those runs in the order they happened (M0 → M9). For each one it covers:
- what was run;
- what happened inside the code;
- how to read the artifact;
- what it shows about Pydantic AI.

Read it next to the code. Every claim points at a file you can open.

> **Start fresh from `main`, not here.** `main` has the code without run artifacts, and `README.md` there explains setup.
> Here, nothing needs to run: every number below comes from a file in `runs/` or `data/runtime/`.

---

## 0. Map of the artifacts

| Artifact | Scenario | Section |
|---|---|---|
| `runs/costs.jsonl` | Every model request ever made: 2,486 lines, $2.13 | [§11](#11-the-cost-ledger-what-everything-cost) |
| `runs/extraction-*.json` (12) | Extractor runs and the output-mode experiment | [§2](#2-m2--extraction-from-a-pdf-to-a-typed-invoice) |
| `runs/audit-*.json` (6) | Auditor runs, from 6 false positives to 0 | [§3](#3-m3--the-auditor-agent-and-its-ledger-tools) |
| `runs/exp-caching-*.json` (2) | Prompt caching on vs off | [§6](#6-m6--caching-and-thinking-experiments) |
| `runs/exp-thinking-*.json` (2) | Thinking off / on escalation / always | [§6](#6-m6--caching-and-thinking-experiments) |
| `runs/crash-*/` (10) | Durable batches killed with `kill -9` and resumed | [§8](#8-m7--crash-tests-a-durable-batch-survives-kill--9) |
| `runs/evals/*.json` | The three eval datasets on Haiku and Sonnet | [§9](#9-m8--evals-online-evaluation-harness-guards) |
| `runs/benchmark-*.json` | The final 40-invoice pipeline run (latency, cost) | [§10](#10-m9--the-benchmark-the-final-state-of-the-pipeline) |
| `data/runtime/app.sqlite`, `ledger.sqlite` | The pipeline state the benchmark left behind | [§4](#4-m4--the-pipeline-graph-one-invoice-end-to-end-per-decision-path), [§10](#10-m9--the-benchmark-the-final-state-of-the-pipeline) |
| `docs/decisions.md` | Why each design choice was made (D0–D24) | throughout |

**Reading the artifacts.** There's no `sqlite3` CLI here, so use Python:

```bash
uv run python -c "import json; d=json.load(open('runs/audit-20261030-043158.json')); print(d['rows'][0])"
uv run python -c "import sqlite3; db=sqlite3.connect('data/runtime/app.sqlite'); print(db.execute('select invoice_id,status from invoices').fetchall())"
uv run invoiceiq costs          # the cost ledger, summarized
uv run invoiceiq postings       # what the pipeline posted
uv run invoiceiq pending        # what's waiting for a person (needs a key in .env: it builds agents)
```

**The corpus** (`data/`, made by `scripts/gen_mockdata.py`): 40 documents (PDFs, scans, one email, one quotation) plus a ledger with vendors, POs and past postings. `data/ground_truth/inv-NNN.json` holds, for each document, the invoice it contains, the expected findings and the expected decision. Every run below is scored against these files.

| Invoices | Scenario | Expected decision |
|---|---|---|
| inv-001…016 | clean, under ₹50k (several layouts, a scan) | auto_approved |
| inv-017…019 | clean, above ₹50k | awaiting_approval |
| inv-020, 021 | exact duplicates of past postings | held |
| inv-022, 023 | fuzzy duplicates (same vendor, amount and date window) | held |
| inv-024…026, 036 | price drift > 5% over the PO | awaiting_approval |
| inv-027 | price drift *within* tolerance | auto_approved |
| inv-028 | blocked vendor | held |
| inv-029 | unknown vendor | awaiting_approval |
| inv-030, 031 | GST type mismatch (IGST vs CGST+SGST) | held |
| inv-032, 033 | arithmetic errors (033 is also stale) | awaiting_approval |
| inv-034, 035, 037 | clean (037 is an email body) | auto_approved |
| inv-038, 039 | USD imports (FX conversion) | awaiting_approval |
| inv-040 | a quotation: not an invoice | rejected |

---

## 1. M0 — the smoke call

**Run:** `uv run python scripts/smoke_live.py`

**Artifact:** line 1 of `runs/costs.jsonl`

```json
{"ts": "2026-10-29T19:41:27+00:00", "label": "m0-smoke", "model": "claude-haiku-5-5", "input": 58, "output": 45, "usd": "0.0000283"}
```

**What it proves:** the key works, the model ID is right (`anthropic:claude-haiku-5-5`, set in `config.py`), and the cost ledger prices a response. Every later line in this file comes from the same function, `record_response` in [src/invoiceiq/shared/cost.py](src/invoiceiq/shared/cost.py). It's called from an `after_model_request` hook ([capabilities/cost.py](src/invoiceiq/capabilities/cost.py)), which is how one `Hooks` capability meters every agent.

---

## 2. M2 — extraction: from a PDF to a typed invoice

**Run:** `uv run python scripts/run_extraction.py`, with `--limit`, `--experiment --mode tool|native|prompted` and `--ids`.

**Artifacts:** `runs/extraction-<mode>-<time>.json`. Each has one row per document: `kind_ok`, fields `correct/total`, the `wrong` fields, `requests`, tokens and `retry_reasons`.

### How one extraction flows

1. [`load_document`](src/invoiceiq/pipeline/documents.py) turns the file into model input:
   - a PDF becomes a `BinaryContent` document;
   - a scan becomes an image (downscaled to ≤1600 px);
   - an `.eml` becomes text.
2. [`build_extractor`](src/invoiceiq/agents/extractor.py) is an `Agent` whose `output_type` is a **union**:
   - `ToolOutput(RawInvoice, name="submit_invoice")`, or
   - `ToolOutput(ExtractionFailure, name="report_failure")`.

   Each member becomes its own output tool, so "is this even an invoice?" is answered by *which tool the model calls*.
3. Dynamic `@agent.instructions` add today's date and our GSTIN (from `RunContext.deps`).
4. An `@agent.output_validator` re-checks the arithmetic and GST rules ([gst.py](src/invoiceiq/gst.py)). On a mismatch it raises `ModelRetry("…")`, and the model gets the message and another try (`retries={'output': 2}`).
5. `UsageLimits(request_limit=4, total_tokens_limit=20_000)` caps runaway runs.

### The runs, in order

| File | Docs | Result | What happened |
|---|---|---|---|
| `extraction-tool-…015301` | 3 | 174/174 fields | First try (`--limit 3`). |
| `extraction-tool-…020046` | 40 | 37 OK, **3 errors** | inv-006, 009 and 036 failed with `UsageLimitExceeded`. |
| `extraction-tool-…020247` / `native-…020522` / `prompted-…020652` | 10 each | 442/442, 442/442, 396/397 + 1 wrong kind | **Output-mode experiment, round 1.** |
| `extraction-tool-…020726` | 1 | 55/55 | inv-036 alone, after the fix. |
| `extraction-tool-…021658` | 40 | 1929/1930 | Full run after the fix. |
| `…021743` / `native-…021842` / `prompted-…021916` | 10 each | all 442/442 | Experiment, round 2. |
| `extraction-tool-…022523` | 40 | **1930/1930, 0 retries** | M2 acceptance. |
| `extraction-tool-…110339` | 1 | 46/46 | M9's observability check (`--ids inv-001 --console`). |

**Lesson 1, thinking eats `max_tokens` (D11).** The 3 errors in `…020046` were Haiku 5.5's *adaptive thinking*, which is on by default and shares the `max_tokens` budget with the answer. The model thought, ran out of room, retried, and blew the 20k token limit. The fix was `Thinking(effort=False)` as a capability on the extractor; thinking is used deliberately later, in §6.

**Lesson 2, output modes (D12).**
- In round 1, `PromptedOutput` (the schema in the prompt) misclassified inv-003, a clean invoice, and got one field wrong.
- In round 2, after the thinking fix, all three modes scored 10/10. Prompted was the cheapest, but ten documents can't separate them.

Tool output stayed the default for three reasons:
- the union decision is visible as which tool was called;
- partial tool arguments are what M5 streams;
- prompted output is only validated after it's generated.

The two tool-mode retries were the model calling a non-existent tool, `invoke`, which is why the output tools were renamed `submit_invoice` and `report_failure`.

**Trace it yourself:** `retry_reasons` lists what each retry was told. In Pydantic AI V2, a validator retry shows up in the message history as a `ToolReturnPart` with `outcome="retried"`, not as a separate message type.

---

## 3. M3 — the auditor agent and its ledger tools

**Run:** `uv run python scripts/run_audit.py` (`--ids`, `-v`, `--no-escalation`)

**Artifacts:** `runs/audit-<time>.json`. Each row has:
- `expected` vs `flagged` finding kinds;
- the `decision` vs `expected_decision`;
- `llm_findings` and the `narrative`;
- `trajectory`: the tools called, in order;
- `escalation` and `fx_loaded`.

### How one audit flows

1. **Deterministic checks run first:**
   - `strict_validate`, `check_duplicates`, `check_po`, `check_vendor` and `check_date_and_fx`, in [pipeline/checks.py](src/invoiceiq/pipeline/checks.py);
   - against the *seed* ledger (`audit_deps(seed=True)`), so evaluations never see the pipeline's own postings.
2. [`build_auditor`](src/invoiceiq/agents/auditor.py) is given the invoice, the rule findings *as facts*, and computed facts (for example "total is above the ₹50k limit").
3. Its tools come from a **custom capability**, [`ledger_capability`](src/invoiceiq/capabilities/ledger.py): `get_vendor`, `get_po`, `vendor_history` and `search_posted`, with usage instructions attached. A capability bundles tools, instructions and hooks; the deps type is a `HasLedger` protocol, so any agent with a ledger can use it.
4. **FX is a *deferred* capability** ([capabilities/fx.py](src/invoiceiq/capabilities/fx.py), `defer_loading=True`):
   - the model sees only a one-line catalog entry;
   - it has to call `load_capability("fx")` to get `convert_to_inr`.

   `fx_loaded` is true **only for inv-038 and inv-039, the two USD invoices**:
   `inv-038: load_capability(fx) → get_vendor → convert_to_inr → vendor_history → search_posted`.
5. The auditor returns an `AuditReport`. `merge_findings` combines its findings with the rule findings, and **`Policy.decide`** ([policy.py](src/invoiceiq/policy.py)), which is pure Python, makes the money decision.

### The runs: precision went from 0.73 to 1.00

| File | Invoices | Recall | Precision | Decisions | What changed |
|---|---|---|---|---|---|
| `audit-…023421` | 3 | — | — | 3/3 | First try. |
| `audit-…023758` | 39 | 16/16 | **16/22** | 35/39 | 6 false positives (below). |
| `audit-…023807` | 1 | 1/1 | 1/1 | 1/1 | One invoice re-checked during the fix. |
| `audit-…024935` | 39 | 16/16 | 16/17 | 38/39 | After the prompt fix and the severity cap. |
| `audit-…042000` | 39 | 16/16 | 16/17 | 38/39 | M6: thinking escalation, first run. |
| `audit-…043158` | 39 | 16/16 | **16/16** | **39/39** | M6 final: 3 escalations. |

**The 6 false positives in `…023758`** are worth reading in the file:
- inv-004 and inv-033: `stale_date`;
- inv-006 and inv-019: `gst_type_mismatch` on a *correct* IGST;
- inv-027: `price_drift` within tolerance;
- inv-039: `unknown_vendor`.

The auditor was treating the list of finding kinds as a checklist to fill.

**Two fixes (D14):**
1. A `checks_passed` field, plus computed facts in the prompt, so the model states what's fine instead of hunting for problems.
2. **LLM findings are capped at "warning".** Only deterministic findings can *hold* an invoice. A model can ask for a person's look; it can't block a payment on its own.

**Lesson:** keep the money decision in code and let the LLM add evidence. Compare `decision` with `suggested` in the rows: the auditor's advice and the policy's decision usually agree, but the policy is the one that counts.

---

## 4. M4 — the pipeline graph: one invoice end to end, per decision path

**Run:** `uv run invoiceiq run --fresh` (one at a time) or `--parallel`.

The final state of a full run is in `data/runtime/`, left by the M9 benchmark (§10).

The graph is in [pipeline/graph.py](src/invoiceiq/pipeline/graph.py), and its diagram, generated from code, is in [docs/pipeline.md](docs/pipeline.md):

```
load_document → extract ─(decision: ExtractionFailure?)→ reject
                       └→ strict_validate → broadcast → [duplicate_check, po_match, vendor_status, date_and_fx] → join
                          → audit → decide ─(decision: policy outcome)→ post | park
```

**Graph features:**
- `GraphBuilder` steps;
- a **decision on the output type** (`g.match(ExtractionFailure)`);
- a **broadcast**: one input fans out to four parallel checks;
- a **join** with `reduce_list_extend`.

Each step's I/O is wrapped in `deps.run_step(...)`, which does nothing in-process and becomes a DBOS step in M7.

### Follow one invoice per path (read `data/runtime/app.sqlite` → `invoices.record_json`)

| Path | Invoice | What the record shows |
|---|---|---|
| **Auto-approve** | inv-001 | `decision.outcome = auto_approved`, `status = posted`, `receipt.posted_by = "policy"` |
| **Approval reviewer (M8)** | inv-017, 018, 019 | `decision = awaiting_approval`, but `status = posted`, `posted_by = "approval-reviewer"` |
| **Needs a person** | inv-024 (price drift) | `status = awaiting_approval`, a row in `pending_approvals` holding the paused `Conversation` |
| **Held** | inv-020 (duplicate), inv-028 (blocked vendor), inv-030 (GST) | `status = held`; approving needs a note |
| **Escalated audit** | inv-027, 029, 038 | `audit_escalation = "auditor disagrees with the rules: …"` |
| **Rejected** | inv-040 | `status = rejected`; the extractor called `report_failure` |

### The approval flow (the core of M4)

1. The poster ([agents/poster.py](src/invoiceiq/agents/poster.py)) has one tool, `post_to_ledger`, registered with **`requires_approval=True`**.
2. When the model calls it, the run **ends**, with `DeferredToolRequests` as its output instead of executing the tool.
3. `request_posting` ([approvals.py](src/invoiceiq/approvals.py)) stores `result.conversation` (messages, usage and the pending call), serialized with `ConversationTypeAdapter`, in `pending_approvals`.
4. **Later, in any process,** `resolve()` loads it, answers the call (`True`, `ToolApproved(override_args={"amount": …})` for a partial posting, or `ToolDenied("why")`), and calls `poster.run(conversation=..., deferred_tool_results=...)`.
5. Auto-approval uses the same path, with `user="policy"`, so the code a person's approval runs is the code the policy runs.

Double posting is impossible for two independent reasons:
- the pending row is claimed atomically;
- the ledger insert is idempotent on a content-derived key.

**Trace it:**
- `uv run invoiceiq pending` shows the 16 waiting invoices;
- the `posted_by` column of `data/runtime/ledger.sqlite` shows who approved each posting (`policy` 20, `approval-reviewer` 3).

---

## 5. M5 — API, streaming, CLI and chat

There are no dedicated run files. Evidence:
- the cost ledger's `ask_ap` lines (12 requests);
- the tests (`tests/test_api.py`, `tests/test_conversation.py`).

The features:
- `POST /invoices` → a background task → `GET /invoices/{id}/stream` (SSE). The stream carries graph steps from `graph.iter()` and, during extraction, the **partial JSON as it grows**: `event_stream_handler` passes the raw tool-call deltas to `from_json(allow_partial=True)`.
- **Why `run(event_stream_handler=...)` and not `run_stream()`:** in V2, `run_stream` can't retry on output validation, and the extractor depends on those retries (D17).
- **Ask-AP chat** ([agents/ask_ap.py](src/invoiceiq/agents/ask_ap.py)) stores each conversation (`Conversation` + `ConversationTypeAdapter`) and continues it with `run(conversation=...)`.

---

## 6. M6 — caching and thinking experiments

### Caching (`runs/exp-caching-*.json`)

Each experiment ran the same documents twice: with the `Caching()` capability, and without it. Anthropic caches *nothing* unless asked.

| Agent | Uncached | Cached | Saving | Why |
|---|---|---|---|---|
| Extractor (10 docs) | $0.01031 | $0.00856 | **17%** | Only the instructions and tool schema repeat (2,219 tokens written once, then read). |
| Auditor (27 requests) | $0.01473 | $0.00792 | **46%** | Multi-step tool runs resend the growing conversation: 91,562 of 118,374 input tokens were cache reads. |

**Lesson:** caching pays most for agents that loop over tools, and least for one-shot calls with unique content.

### Thinking (`runs/exp-thinking-…041611.json`)

12 hard audits, three policies:

| Policy | False positives | Correct decisions | Reasoning tokens | Cost |
|---|---|---|---|---|
| Off | 3 | 9/12 | 0 | $0.0067 |
| **Escalate only on disagreement** | **0** | **12/12** | 2,832 | $0.0100 |
| Always | 0 | 12/12 | 8,340 | $0.0116 |

**How escalation works** (`run_audit` in [agents/auditor.py](src/invoiceiq/agents/auditor.py)):
1. Audit once with thinking off.
2. If the auditor contradicts a deterministic rule (`escalation_reason`), run again with `capabilities=[Thinking("medium")]` *per run*, plus more `max_tokens`.

Only 3 of 39 invoices pay for reasoning. In `audit-…043158.json`, look at inv-019, 027 and 029: the `escalation` field says why each was escalated.

**Lesson (D20):** with thinking on, Pydantic AI switches to strict `NativeOutput`, under which free-form `dict` fields come back empty. Evidence is therefore modeled as a list of `Evidence(name, value)` pairs.

### MCP (no run file; it shows in the `ask_ap` cost lines)

The ledger is also served as an MCP server ([mcp_server.py](src/invoiceiq/mcp_server.py), `MCPServer` from `mcp` 2.x). Ask-AP uses it through the `MCP` capability over streamable HTTP, and its answer (a vendor's spend, INR 36,981.17) was checked against the ledger.

---

## 7. M7 — reliability

Covered by `tests/test_reliability.py` (fault injection with `FunctionModel`s), not by live runs. Each scenario maps to one feature:
- **429 from Haiku:** `FallbackModel(..., fallback_on=TRANSIENT)` answers with Sonnet.
- **401 (bad key):** *doesn't* fall back, because every model would fail the same way.
- **Two invalid outputs, then a valid one:** succeeds through output retries. A third invalid output fails as `invalid_output`.
- **A slow model:** `run(timeout=)` raises `RunTimedOut`.
- **A runaway loop:** `UsageLimits` stops it.
- **Concurrency:** `ConcurrencyLimitedModel` caps requests in flight across all agents.
- **Any failure inside the pipeline** becomes a stored `failed` record with a category (`describe_failure`), not a crash.

---

## 8. M7 — crash tests: a durable batch survives `kill -9`

**Run:** `uv run python scripts/crash_test.py` (fake models, $0) or `--live`.

**What the script does** ([scripts/crash_test.py](scripts/crash_test.py)):
1. Copies `data/` into `runs/crash-<time>/` (the copies are not committed here).
2. Starts a worker process that submits a 40-invoice DBOS batch.
3. Once 15 invoices have finished, sends `kill -9`: no cleanup, no flush.
4. Starts a second worker. `DBOS.launch()` finds the unfinished workflows and resumes them.
5. Checks the books:
   - every invoice finished;
   - one posting per posted invoice, with no idempotency key used twice;
   - no model calls after the kill for invoices that had finished before it.

**The durable design** ([pipeline/batch.py](src/invoiceiq/pipeline/batch.py)):
- One parent workflow `invoiceiq_process_batch`, plus a child workflow per invoice (`{batch_id}:{invoice_id}`) on a DBOS queue with `worker_concurrency=4`.
- Inside a child, `DBOSDurability` makes every **model request** a checkpointed step, and `deps.run_step` → `DBOS.run_step_async` does the same for our own I/O.
- On recovery, completed steps return their recorded results instantly; only the step that was in flight runs again.

### The 10 runs

| Folder | Mode | Result | What it shows |
|---|---|---|---|
| `crash-…045006` | first attempt | worker crashed at import | `worker1.log`: dbos 3.x refuses `Queue(...)`; queues must be registered after `DBOS.launch()` (D21). |
| `crash-…045116`, `…045221`, `…045304` | fake, 12 invoices | pass | Building the test; see `calls.jsonl`, the fakes' call log with pid and time. |
| `crash-…045803`, `…085948` | fake, 40 | **pass** | 40/40 decided, 20 postings, 0 duplicate keys, no re-spend. |
| **`crash-…090157`** | **LIVE** | **FAIL** | The run worth reading (below). |
| `crash-…091751` | fake, 40 | pass | After the fix; the fake auditor now escalates inv-014. |
| **`crash-…093638`** | **LIVE** | **PASS** | M7 acceptance: 40/40, 0 double posts, no re-spend; inv-027 escalated durably. |
| `crash-…100609` | fake, 40 | pass | Regression check after M8 (online evaluation inside DBOS). |

### Reading `crash-…090157` (the failed live run, D22)

1. **`worker2.log`:**
   - `Recovering 5 workflows from application version invoiceiq-p1`: DBOS found the batch and the 4 in-flight children.
   - Then the `RESULT` line: inv-018, 027 and 038 are `failed` with *"Capabilities added per-run inside a DBOS workflow are not supported: Thinking"*.
   - **Cause:** escalation adds `Thinking` per run, and DBOS registers an agent's durable steps when the agent is built.
   - **Fix:** a second agent, `auditor_thinking`, with thinking fixed at construction.
2. **The "re-spend" it reported was mostly double counting.** Search `runs/costs.jsonl` for inv-016 around `03:33`: its extractor and auditor lines after the kill are **byte-identical** to the ones before (same tokens, same cache split).
   - DBOS *replayed* the recorded responses, and no API call was made.
   - But the `after_model_request` hook runs outside the checkpointed step, so it logged them again.
   - **Fix:** the ledger skips a response whose `provider_response_id` it already has.
   - In total, 6 lines (about $0.003) are phantom.
3. **One repeat was real and correct.** inv-016's poster call ran again: its record had been saved as `posted` inside the `post` step, but the kill landed before DBOS checkpointed that step. The ledger's idempotency key returned the original posting.

   The test's own definition was also fixed: "finished before the kill" now means the DBOS workflow is `SUCCESS` in `workflow_status`, not "has a final record".

Each crash folder's `data/runtime/` has the full state:
- `dbos.sqlite`: the `workflow_status` and `operation_outputs` tables, i.e. the checkpoints themselves;
- `app.sqlite`: the records;
- `ledger.sqlite`: the postings.

---

## 9. M8 — evals, online evaluation, Harness guards

**Run:** `uv run python -m evals.extraction | evals.audit | evals.narrative [--final]`, then `uv run python -m evals.compare`.

**Artifacts:** `runs/evals/<dataset>-<model>-<time>.json`, plus `latest-…` copies. `evals/baseline.json` is the accepted baseline (on `main` too).

### What each dataset measures ([evals/](evals/))

**Extraction** (40 documents):
- custom evaluators: `KindCorrect`, `FieldAccuracy` (±₹1, with a score per header field) and `LineItemF1`;
- built-in: `MaxModelRequests(3)`.

**Audit** (39 invoices; the decision path, not the bare agent):
- `FindingRecall`, `FindingPrecision` and `DecisionMatch`;
- `MaxToolCalls(8)`;
- per case: `ToolCorrectness(["get_po"])` on price-drift cases, and `TrajectoryMatch(["get_vendor","search_posted"])` on duplicate cases.

**Narrative** (10 cases): an `LLMJudge` with a 4-point rubric, next to a plain sentence-count rule.

**For every dataset:**
- **`SpansCaptured`.** The agentic evaluators read tool calls from OpenTelemetry spans; without Logfire configured they'd all score 0 silently, so the run aborts instead.
- **A `ReportEvaluator`** computes the micro-averaged headline metrics that `evals.compare` gates on: it fails on a drop of more than 3 points.

### Results

| | Haiku | Sonnet |
|---|---|---|
| extraction: field / kind accuracy | 1.00 / 1.00 | 1.00 / 1.00 |
| audit: recall / precision / decisions | 1.00 / 1.00 / 1.00 | 1.00 / 1.00 / 1.00 |
| narrative: judge pass rate | 0.80 (inv-033 and 038 failed) | — |
| cost, extraction + audit | $0.058 | $1.27 |

**What to notice in the files:**
- **`per_case` → `TrajectoryMatch` is 0.4–0.8 on the duplicate cases**, even though the auditor investigated correctly. `in_order` is an F1 score over the longest common subsequence, so every extra tool call (like `get_po`) lowers it. The metric was misleading; the behavior was fine.
- **Sonnet bought nothing on this corpus**, for about 22× the price.
- **The report's `requests` and token metrics are double the ledger's:** 2 `chat` spans per live request. The `cost` metric is correct. This is the open item in `docs/observability.md`.

### Online evaluation and guards (exercised in §10's run)

- **`OnlineEvaluation` on the auditor** ([online_evals.py](src/invoiceiq/online_evals.py)) samples 20% of real runs and emits `gen_ai.evaluation.result` events for `MaxToolCalls` and `NarrativeNotEmpty`.
- **`ApprovalReviewer`** ([capabilities/guards.py](src/invoiceiq/capabilities/guards.py)) answers pending approvals in one tier only: high value, no findings. It can only *approve*.
- **`ToolCallJudge`** runs when `post_to_ledger` is about to execute, after any approval. It *blocks* a call that would post more than the invoice total, and partial postings pass.
- **Their own model calls bypass the host agent's capabilities.** The Harness builds internal agents, so `CostRecordingModel` wraps their model to keep the ledger complete. That's where the `amount_judge` and `approval_reviewer` cost lines come from.

---

## 10. M9 — the benchmark: the final state of the pipeline

**Run:** `uv run python scripts/benchmark.py`. It runs all 40 invoices one at a time on a fresh runtime, with every M1–M8 feature on.

**Artifact:** `runs/benchmark-claude-haiku-5-5-20261030-110239.json`, plus the whole of `data/runtime/`.

| Metric | Value |
|---|---|
| Decisions matching ground truth | **40/40** |
| Cost per invoice | **$0.00173**, of which: auditor $0.031, extractor $0.029, poster $0.0067, judge $0.0026 and reviewer $0.0003, over 40 invoices |
| Latency per invoice, p50 / p95 / max | **14.6 s / 20.2 s / 22.4 s** |
| Outcomes | 23 posted (20 by policy, 3 by the reviewer), 9 awaiting approval, 7 held, 1 rejected |

Everything from §4 can be read in `data/runtime/` exactly as this run left it: 16 rows in `pending_approvals` waiting for a person, 23 postings, and escalations on inv-027, 029 and 038.

---

## 11. The cost ledger: what everything cost

`runs/costs.jsonl` has 2,486 requests from 2026-10-29 19:41 to 2026-10-30 05:33 UTC. **Total: $2.13** (about $2.12 after the 6 phantom replay lines from §8). The project's cap was $6.

| Label | Requests | Cost | Mostly from |
|---|---|---|---|
| auditor | 1,481 | $1.07 | M3/M6 audit runs, the experiments, crash tests, the M8 Sonnet eval ($0.69) |
| extractor | 577 | $1.01 | M2 runs, the M8 Sonnet eval ($0.59) |
| poster | 377 | $0.041 | M4 pipeline runs, crash tests |
| amount_judge, approval_reviewer, narrative_judge | 23 / 3 / 10 | $0.005 | M8 guards and judge |
| ask_ap | 12 | $0.0016 | M5/M6 chat |
| auditor_thinking | 2 | $0.0013 | The durable escalation in the passing live crash test |
| m0-smoke | 1 | $0.00003 | M0 |

**Where the money went:** the single Sonnet eval pass cost more than every Haiku run in the project combined. Haiku 5.5 at $0.10/$0.50 per million tokens makes a full 40-invoice run cost about 7 cents.

**Fields in each line:**
- `label`: the agent's `name`;
- `invoice`: from `run(metadata={"invoice_id": …})`, since M7;
- `cache_read`, `cache_write` and `reasoning` tokens;
- `response_id`: since D22, the key that stops a replayed response being counted twice.

---

## 12. Where to go next

- `docs/decisions.md` has the reasoning behind every choice above (D0–D24).
- `notes/p1-journal.md` has the 12 learning questions. This branch is the evidence to answer them with.
- To re-run any scenario, check out `main`, `cp .env.example .env`, add your key, and use the command at the top of each section.
