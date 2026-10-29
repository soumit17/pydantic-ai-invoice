"""Global $0 guard (spec §16.1): no test may reach a real model."""

from __future__ import annotations

import os

# Must run before anything builds an 'anthropic:' agent: construction fails without a key (spec §2.1).
os.environ["ANTHROPIC_API_KEY"] = "test-dummy"
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"
os.environ["LOGFIRE_TOKEN"] = ""

import subprocess  # noqa: E402
import sys  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from pydantic_ai import models  # noqa: E402

from invoiceiq.config import PROJECT_ROOT, Settings, get_settings  # noqa: E402

# Any real model request now raises RuntimeError. TestModel and FunctionModel are exempt.
models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    """Settings with data and run folders redirected into a temp dir, so tests never touch real files."""
    get_settings.cache_clear()
    s = Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")  # pyright: ignore[reportCallIssue]
    yield s
    get_settings.cache_clear()


GENERATOR = PROJECT_ROOT / "scripts" / "gen_mockdata.py"


def generate(out: Path) -> None:
    subprocess.run([sys.executable, str(GENERATOR), "--out", str(out)], check=True, capture_output=True)


@pytest.fixture(scope="session")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The full mock corpus and ledger, generated once per test session into a temp dir."""
    out = tmp_path_factory.mktemp("mock")
    generate(out)
    return out
