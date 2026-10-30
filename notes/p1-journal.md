# P1 learning journal

Your words, not Claude's (spec §23). Fill these in as you go.

## M2 experiment: output modes (10 invoices, `--experiment`)

Haiku 5.5, thinking off, one run each on 2026-10-30 (`runs/extraction-*-20261030-021*.json`).

| Mode | Kind correct | Field accuracy | Retries | Input tokens (cached) | Output tokens | Cost | Failure modes seen |
|---|---|---|---|---|---|---|---|
| tool | 10/10 | 442/442 | 2 | 56,266 (≈2.2k/request) | 10,315 | $0.0084 | |
| native | 10/10 | 442/442 | 2 | 64,903 (≈3.1k/request) | 7,930 | $0.0075 | |
| prompted | 10/10 | 442/442 | 0 | 42,146 (≈2.1k/request) | 6,591 | $0.0058 | |

Before the fix (thinking on, max_tokens 2048 truncating): tool 10/10, 3 retries, $0.0131; native 10/10,
2 retries, $0.0116; prompted 9/10 (inv-003 returned as a failure), 2 retries, $0.0111.

## M6 experiment: prompt caching (`scripts/exp_caching.py`)

| Agent | Invoices | Cache read / input tokens | Cache write tokens | Cost cached | Cost uncached | Saving |
|---|---|---|---|---|---|---|
| extractor | 10 | 19,971 / 47,466 (42%) | 2,219 | $0.0086 | $0.0103 | 17% |
| auditor | 10 | 91,562 / 118,374 (77%) | 26,468 | $0.0079 | $0.0147 | 46% |

## M6 experiment: Thinking escalation (`scripts/exp_thinking.py`)

| Mode | Audits | False positives | Decisions right | Escalated | Reasoning tokens/audit | $/audit |
|---|---|---|---|---|---|---|
| off | 12 | 3 | 9/12 | 0 | 0 | $0.00056 |
| escalation | 12 | 0 | 12/12 | 3 | 236 | $0.00083 |
| always | 12 | 0 | 12/12 | 0 | 695 | $0.00097 |

## M8 evals: Haiku vs Sonnet (`uv run python -m evals.<name>`, then `--final`)

| Dataset | Metric | Haiku | Sonnet | Δ |
|---|---|---|---|---|
| extraction (40) | field_accuracy | | | |
| extraction (40) | kind_accuracy | | | |
| extraction (40) | cost | | | |
| audit (36) | recall | | | |
| audit (36) | precision | | | |
| audit (36) | decision_accuracy | | | |
| audit (36) | cost | | | |
| narrative (10) | narrative_pass_rate (LLM judge) | | | |
| narrative (10) | four_sentences_rate (plain rule) | | | |

Accuracy delta vs cost delta: is Sonnet worth it for either agent?

## M8 Harness guards

- ApprovalReviewer protects against: …
- ToolCallJudge protects against: …
- Where the LLM judge and the deterministic `amount <= total` check overlap, which would you keep?

## Questions

1. When did validation-as-retry help, and when did it hide real problems? (§7.1)
2. Tool output vs `NativeOutput` vs `PromptedOutput`: which won on your data, and why?
3. What exactly is a capability, and how is it different from a toolset?
4. What did `Caching` save in practice? When would it *cost* more?
5. Conversation storage vs durable execution: what problem does each solve?
6. Why is the money decision pure Python and not the LLM?
7. Deferred tools vs blocking for input: what does the stateless approach buy you?
8. GraphBuilder vs a single agent loop: where is the line?
9. What surprised you about evals? Which metric was misleading at first?
10. Core vs Harness: what would you put in production today?
11. Cost per invoice (Haiku vs Sonnet), and the p50/p95 latency.
12. If you had to rebuild this in LangGraph (P4), what would be easier or harder?
