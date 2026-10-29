"""Logfire / OpenTelemetry setup (spec §15).

Logfire is plain OpenTelemetry: with no LOGFIRE_TOKEN nothing leaves the machine, and
spans still exist in-process (evals and local exporters rely on that).
"""

from __future__ import annotations

import logfire

from invoiceiq.config import Settings

_configured = False


def configure_telemetry(settings: Settings, *, console: bool = False) -> None:
    """Idempotent. Call once at process start (CLI, API, scripts, evals)."""
    global _configured
    if _configured:
        return
    token = settings.logfire_token.get_secret_value() if settings.logfire_token else None
    logfire.configure(
        service_name="invoiceiq",
        token=token,
        send_to_logfire="if-token-present",
        # console=False keeps script output clean; pass console=True to see spans in the terminal.
        console=None if console else False,
        scrubbing=False,  # PII redaction is our own PIIRedactionHooks (M2), not Logfire's scrubber.
    )
    # Emits agent-run / model-request / tool spans, and silences the first-run banner.
    logfire.instrument_pydantic_ai()
    _configured = True
