"""Append-only cost ledger shared by every live call in the project.

Each model response becomes one JSON line in `runs/costs.jsonl`. Prices come from
`ModelResponse.cost()`, which uses Pydantic's `genai-prices` tables, so we never hard-code rates.
`CostHooks` (M2) calls `record_response` from an `after_model_request` hook; M0's smoke script
calls it directly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelResponse

PROJECT = "p1"


class BudgetExceeded(RuntimeError):
    """Raised before a run when cumulative spend has reached the project cap."""


def record_response(
    response: ModelResponse, *, path: Path, project: str = PROJECT, label: str = ""
) -> Decimal:
    """Price one model response and append it to the ledger. Returns the cost in USD."""
    usage = response.usage
    try:
        cost = response.cost().total_price
    except (LookupError, ValueError, AssertionError):
        # Unknown model in genai-prices, or no usage reported: log it but count it as zero.
        cost = Decimal(0)
    entry: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": project,
        "label": label,
        "model": response.model_name,
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "reasoning": usage.output_reasoning_tokens,  # thinking tokens, billed as output
        "cache_read": usage.cache_read_tokens,
        "cache_write": usage.cache_write_tokens,
        "usd": str(cost),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return cost


def total_spend(*, path: Path, project: str = PROJECT) -> Decimal:
    """Sum of every recorded cost for `project`."""
    if not path.exists():
        return Decimal(0)
    total = Decimal(0)
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                row: dict[str, Any] = json.loads(line)
                if row.get("project") == project:
                    total += Decimal(str(row["usd"]))
    return total


def check_budget(*, path: Path, cap_usd: float, project: str = PROJECT) -> Decimal:
    """Raise `BudgetExceeded` if spend has reached the cap; otherwise return spend so far."""
    spent = total_spend(path=path, project=project)
    if spent >= Decimal(str(cap_usd)):
        raise BudgetExceeded(f"{project} has spent ${spent:.4f}, cap is ${cap_usd:.2f}")
    return spent


def summary(*, path: Path, cap_usd: float, project: str = PROJECT) -> dict[str, Any]:
    """Spend so far, split by label (agent or script) and by model."""
    by_label: dict[str, Decimal] = {}
    by_model: dict[str, Decimal] = {}
    requests = 0
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row: dict[str, Any] = json.loads(line)
                if row.get("project") != project:
                    continue
                usd = Decimal(str(row["usd"]))
                requests += 1
                by_label[row.get("label") or "-"] = by_label.get(row.get("label") or "-", Decimal(0)) + usd
                by_model[row.get("model") or "-"] = by_model.get(row.get("model") or "-", Decimal(0)) + usd
    total = sum(by_label.values(), Decimal(0))
    return {
        "total_usd": str(total),
        "cap_usd": str(cap_usd),
        "remaining_usd": str(Decimal(str(cap_usd)) - total),
        "requests": requests,
        "by_label": {k: str(v) for k, v in sorted(by_label.items())},
        "by_model": {k: str(v) for k, v in sorted(by_model.items())},
    }
