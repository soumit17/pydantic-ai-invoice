"""LIVE: run the extractor over the mock invoices and score it against ground truth.

    uv run python scripts/run_extraction.py --limit 3                 # cheap first try
    uv run python scripts/run_extraction.py                           # all 40 (M2 acceptance)
    uv run python scripts/run_extraction.py --experiment --mode tool  # the 10-invoice output-mode
    uv run python scripts/run_extraction.py --experiment --mode native
    uv run python scripts/run_extraction.py --experiment --mode prompted
    uv run python scripts/run_extraction.py --ids inv-036 -v          # one file, show wrong fields

Add --console to watch the spans (agent run → model requests → retries) in the terminal.
Each run writes runs/extraction-<mode>-<time>.json for your journal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelRequest, RetryPromptPart, ToolReturnPart

from invoiceiq.agents.extractor import EXTRACT_LIMITS, OutputMode, build_extractor, extract_deps
from invoiceiq.config import get_settings
from invoiceiq.models import GroundTruth
from invoiceiq.pipeline.documents import load_document
from invoiceiq.scoring import score
from invoiceiq.shared.cost import check_budget, total_spend
from invoiceiq.telemetry import configure_telemetry

# 10 varied documents for the output-mode experiment: every layout, a scan, the email,
# a USD import, a math error, a GST mismatch, a fuzzy duplicate and the quotation.
EXPERIMENT_IDS = [
    "inv-001", "inv-002", "inv-003", "inv-023", "inv-030",
    "inv-032", "inv-034", "inv-037", "inv-038", "inv-040",
]  # fmt: skip


def retry_reasons(messages: list[ModelMessage]) -> list[str]:
    """What the model was told before each retry. In V2 an output-tool retry is the tool's return
    value carrying the error; other retries (e.g. a truncated reply) are RetryPromptParts."""
    reasons: list[str] = []
    for m in messages[1:]:
        if isinstance(m, ModelRequest):
            for part in m.parts:
                if isinstance(part, RetryPromptPart):
                    reasons.append(part.model_response()[:300])
                elif isinstance(part, ToolReturnPart) and part.content != "Final result processed.":
                    reasons.append(str(part.content)[:300])
    return reasons


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["tool", "native", "prompted"], default="tool")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--experiment", action="store_true", help="the fixed 10-invoice set")
    parser.add_argument("--final", action="store_true", help="use MODEL_FINAL (Sonnet) instead of Haiku")
    parser.add_argument("--console", action="store_true", help="print spans to the terminal")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every wrong field")
    args = parser.parse_args()

    settings = get_settings()
    configure_telemetry(settings, console=args.console)
    spent_before = check_budget(path=settings.costs_path, cap_usd=settings.spend_cap_usd)

    truth_dir = settings.data_dir / "ground_truth"
    truths = [GroundTruth.model_validate_json(p.read_text()) for p in sorted(truth_dir.glob("*.json"))]
    if args.experiment:
        truths = [t for t in truths if t.file_id in EXPERIMENT_IDS]
    if args.ids:
        truths = [t for t in truths if t.file_id in args.ids]
    if args.limit:
        truths = truths[: args.limit]

    mode: OutputMode = args.mode
    model = settings.model_final if args.final else settings.model_dev
    agent = build_extractor(settings, model=model, output_mode=mode)
    deps = extract_deps(settings)
    print(
        f"extracting {len(truths)} document(s) with {model}, {mode} output; "
        f"spent so far ${spent_before:.4f}\n"
    )

    rows: list[dict[str, Any]] = []
    for t in truths:
        prompt = load_document(settings.data_dir / "invoices" / t.file_name)
        start = time.perf_counter()
        try:
            result = await agent.run(prompt, deps=deps, usage_limits=EXTRACT_LIMITS)
        except (UnexpectedModelBehavior, UsageLimitExceeded) as e:
            print(f"ERR  {t.file_id} {t.category_name:<24} {type(e).__name__}: {e}")
            rows.append({"file_id": t.file_id, "error": f"{type(e).__name__}: {e}"})
            continue
        seconds = time.perf_counter() - start
        s = score(t.expected, result.output)
        usage = result.usage
        retries = usage.requests - 1
        flag = "OK " if s.kind_ok else "BAD"
        print(
            f"{flag}  {t.file_id} {t.category_name:<24} {result.output.kind:<8} "
            f"fields {s.correct:>3}/{s.total:<3} ({s.accuracy:6.1%})  "
            f"req {usage.requests} in {usage.input_tokens:>5} (cached {usage.cache_read_tokens:>5}) "
            f"out {usage.output_tokens:>5} (think {usage.output_reasoning_tokens:>4})  {seconds:4.1f}s"
            + (f"  ({retries} retr{'y' if retries == 1 else 'ies'})" if retries else "")
        )
        if args.verbose:
            for w in s.wrong:
                print(f"       ✗ {w}")
            for reason in retry_reasons(result.all_messages()):
                print(f"       ↻ retry because: {reason}")
        rows.append({
            "file_id": t.file_id, "category": t.category_name, "kind_ok": s.kind_ok,
            "correct": s.correct, "total": s.total, "wrong": s.wrong, "requests": usage.requests,
            "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens, "reasoning_tokens": usage.output_reasoning_tokens,
            "retry_reasons": retry_reasons(result.all_messages()), "seconds": round(seconds, 2),
        })  # fmt: skip

    done = [r for r in rows if "error" not in r]
    kinds_ok = sum(r["kind_ok"] for r in done)
    correct = sum(r["correct"] for r in done)
    total = sum(r["total"] for r in done)
    spent = total_spend(path=settings.costs_path) - spent_before
    n = max(len(done), 1)
    print(f"\nkind correct:   {kinds_ok}/{len(rows)}")
    print(f"field accuracy: {correct}/{total} = {correct / max(total, 1):.1%}")
    print(f"errors:         {len(rows) - len(done)}")
    print(f"retries:        {sum(r['requests'] - 1 for r in done)}")
    print(f"thinking:       {sum(r['reasoning_tokens'] for r in done)} output tokens")
    print(
        f"tokens:         in {sum(r['input_tokens'] for r in done)}, "
        f"out {sum(r['output_tokens'] for r in done)}"
    )
    print(f"cost:           ${spent:.4f} (${spent / n:.5f} per document)")

    out = settings.runs_dir / f"extraction-{mode}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = {"model": model, "mode": mode, "cost_usd": str(spent), "rows": rows}
    out.write_text(json.dumps(summary, indent=2, default=lambda o: str(o) if isinstance(o, Decimal) else o))
    print(f"details:        {out.relative_to(settings.runs_dir.parent)}")


if __name__ == "__main__":
    asyncio.run(main())
