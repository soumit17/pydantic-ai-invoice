"""Global $0 guard (spec §16.1): no test may reach a real model."""

from __future__ import annotations

import os

# Must run before anything builds an 'anthropic:' agent: construction fails without a key (spec §2.1).
os.environ["ANTHROPIC_API_KEY"] = "test-dummy"
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"
os.environ["LOGFIRE_TOKEN"] = ""

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from pydantic_ai import models  # noqa: E402

from invoiceiq.config import Settings, get_settings  # noqa: E402

# Any real model request now raises RuntimeError. TestModel and FunctionModel are exempt.
models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    """Settings with data and run folders redirected into a temp dir, so tests never touch real files."""
    get_settings.cache_clear()
    s = Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")  # pyright: ignore[reportCallIssue]
    yield s
    get_settings.cache_clear()
