"""LIVE: one real model call, priced and logged to runs/costs.jsonl. Costs well under $0.001.

    uv run python scripts/smoke_live.py
    uv run python scripts/smoke_live.py --console   # also print OTel spans to the terminal
"""

from __future__ import annotations

import sys

from invoiceiq.agents.hello import build_hello_agent
from invoiceiq.config import get_settings
from invoiceiq.shared.cost import check_budget, record_response, total_spend
from invoiceiq.telemetry import configure_telemetry


def main() -> None:
    settings = get_settings()
    if settings.anthropic_api_key is None:
        sys.exit("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
    configure_telemetry(settings, console="--console" in sys.argv)

    spent = check_budget(path=settings.costs_path, cap_usd=settings.spend_cap_usd)
    print(f"spend so far: ${spent:.6f} of ${settings.spend_cap_usd:.2f}")

    agent = build_hello_agent(settings)
    result = agent.run_sync("Say hello and name one thing a GSTIN identifies.")

    cost = record_response(result.response, path=settings.costs_path, label="m0-smoke")
    usage = result.usage  # a property in V2, not a method
    print(f"model:  {result.response.model_name}")
    print(f"output: {result.output}")
    print(f"tokens: in={usage.input_tokens} out={usage.output_tokens} requests={usage.requests}")
    print(f"cost:   ${cost:.6f}   total now: ${total_spend(path=settings.costs_path):.6f}")


if __name__ == "__main__":
    main()
