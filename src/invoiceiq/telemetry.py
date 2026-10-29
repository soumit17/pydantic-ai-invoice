"""Logfire / OpenTelemetry setup (spec §15).

Logfire is plain OpenTelemetry: with no LOGFIRE_TOKEN nothing leaves the machine, and
spans still exist in-process (evals and local exporters rely on that).
"""

from __future__ import annotations

import logging

import logfire

from invoiceiq.capabilities.pii import scrubbing_options
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
        # Mask bank account and phone numbers inside span attributes (prompts, outputs, tool args).
        scrubbing=scrubbing_options(),
        # Ask-AP's MCP client passes trace context to the ledger MCP server on purpose, so the
        # server's tool spans nest under the agent run. Declare it, or Logfire warns (D18).
        distributed_tracing=True,
    )
    # Emits agent-run / model-request / tool spans, and silences the first-run banner.
    # include_binary_content=False keeps base64 PDFs and scans out of the traces.
    logfire.instrument_pydantic_ai(include_binary_content=False)
    # Our own loggers (cost ledger, PII audit log) become Logfire logs inside the current span.
    app_log = logging.getLogger("invoiceiq")
    app_log.setLevel(logging.INFO)
    app_log.addHandler(logfire.LogfireLoggingHandler())
    _configured = True
