"""FxCapability (spec §8.5): currency conversion, loaded only when the model asks for it.

`defer_loading=True` keeps the tool and its instructions out of every request. The model sees a
one-line catalog entry ("fx: Currency conversion ...") and calls the built-in `load_capability`
tool when it needs it. Only the USD invoices should ever trigger that; INR invoices never pay for
the tool definition.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic_ai import ModelRetry
from pydantic_ai.capabilities import Capability

from invoiceiq.policy import FX_RATES_INR, to_inr

FX_ID = "fx"

fx_capability = Capability[object](
    id=FX_ID,
    description="Currency conversion to INR, for invoices that are not in INR.",
    instructions="Convert foreign-currency amounts with convert_to_inr before quoting them in INR.",
    defer_loading=True,
)


@fx_capability.tool_plain
def convert_to_inr(amount: Decimal, currency: str, on_date: date) -> Decimal:
    """Convert an amount to INR at the reference rate for `on_date` (the invoice date)."""
    currency = currency.strip().upper()
    if currency not in FX_RATES_INR:
        raise ModelRetry(f"No rate for {currency!r}. Supported: {', '.join(sorted(FX_RATES_INR))}.")
    return to_inr(amount, currency)  # static mock table: the date is accepted but not used
