"""PII redaction (spec §8.3): bank account and phone numbers never reach our logs or traces.

The model still sees the raw document; only what we *record* is redacted. Two layers:
- `build_pii_hooks()`: a `Hooks` capability that writes a redacted audit log of each request and
  response. Hooks may observe without mutating: both return their input unchanged.
- `scrubbing_options()`: Logfire's native scrubber, with a callback that applies the same
  `redact()` to the spans Pydantic AI's instrumentation emits (prompts, outputs, tool args).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import logfire
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Hooks
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext

log = logging.getLogger("invoiceiq.audit")

# A 9-18 digit number shortly after "A/c" or "Account" (with optional "No", "Number", "#", ":").
# No inline (?i): Logfire joins every scrubbing pattern into one regex, where inline flags are illegal.
ACCOUNT_RE = re.compile(
    r"\b([Aa]/[Cc]|[Aa]ccount)(\s*(?:[Nn]o\.?|[Nn]umber|#)?\s*[:.\-]?\s*)(\d{9,18})\b"
)  # International phone numbers: +91 98765 43210, +1 510 555 1234, ...
PHONE_RE = re.compile(r"\+\d{1,3}(?:[\s-]?\d{2,5}){2,4}")


def redact(text: str) -> str:
    text = ACCOUNT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED-ACCOUNT]", text)
    return PHONE_RE.sub("[REDACTED-PHONE]", text)


def contains_pii(text: str) -> bool:
    return bool(ACCOUNT_RE.search(text) or PHONE_RE.search(text))


# --- Logfire scrubbing --------------------------------------------------------------------------


def _scrub_callback(match: logfire.ScrubMatch) -> Any:
    # Called for every match of any scrubbing pattern. For our PII patterns, return the value with
    # just the numbers masked; for Logfire's defaults (password, api key, ...) return None, which
    # keeps Logfire's normal behaviour of replacing the whole value.
    if isinstance(match.value, str) and contains_pii(match.value):
        return redact(match.value)
    return None


def scrubbing_options() -> logfire.ScrubbingOptions:
    return logfire.ScrubbingOptions(
        extra_patterns=[ACCOUNT_RE.pattern, PHONE_RE.pattern], callback=_scrub_callback
    )


# --- Audit-log hooks ----------------------------------------------------------------------------


def _request_text(request_context: ModelRequestContext) -> str:
    """The text parts of the newest request (binary documents are skipped)."""
    parts: list[str] = []
    last = request_context.messages[-1] if request_context.messages else None
    if isinstance(last, ModelRequest):
        for part in last.parts:
            if isinstance(part, UserPromptPart):
                content = part.content
                if isinstance(content, str):
                    parts.append(content)
                else:
                    parts.extend(c for c in content if isinstance(c, str))
    return "\n".join(parts)


def _response_text(response: ModelResponse) -> str:
    parts: list[str] = []
    for part in response.parts:
        if isinstance(part, TextPart):
            parts.append(part.content)
        elif isinstance(part, ToolCallPart):
            parts.append(f"{part.tool_name}({part.args_as_json_str()})")
    return "\n".join(parts)


def build_pii_hooks() -> Hooks[object]:
    hooks: Hooks[object] = Hooks(id="pii_redaction", description="Redacted audit log of prompts and outputs")

    @hooks.on.before_model_request
    async def audit_request(
        ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if text := _request_text(request_context):
            log.info("prompt: %s", redact(text)[:2000])
        return request_context  # unchanged: the model still sees the original document

    @hooks.on.after_model_request
    async def audit_response(
        ctx: RunContext[Any], *, request_context: ModelRequestContext, response: ModelResponse
    ) -> ModelResponse:
        log.info("output: %s", redact(_response_text(response))[:2000])
        return response

    return hooks
