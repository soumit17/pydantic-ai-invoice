"""M0 hello-world agent: the smallest useful Agent, used by the smoke script and the first test."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

from invoiceiq.config import Settings


def build_hello_agent(settings: Settings) -> Agent[object, str]:
    # Agent[Deps, Output]: no deps here, and V2's generic default for deps is `object` (not None).
    # A factory instead of a module-level agent: building an 'anthropic:' agent needs the API key at
    # construction time, so importing this module stays safe in tests.
    return Agent(
        settings.model_dev,
        output_type=str,
        name="hello",
        description="M0 smoke-test agent: one short reply.",
        instructions="You are a terse assistant for an Indian accounts-payable team. Reply in one sentence.",
        model_settings=ModelSettings(max_tokens=64),
    )
