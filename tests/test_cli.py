"""The CLI's first-run experience (M9): a fresh clone without a key gets one clear line, not a traceback."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from invoiceiq import cli
from invoiceiq.config import Settings


@pytest.mark.parametrize("args", [["serve"], ["run", "inv-001"], ["pending"]])
def test_commands_that_build_agents_explain_a_missing_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str]
) -> None:
    # As in a fresh clone: no exported key, and no .env (the real one is read by absolute path).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    no_env = Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")  # pyright: ignore[reportCallIssue]
    monkeypatch.setattr(cli, "get_settings", lambda: no_env)
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 2
    assert "ANTHROPIC_API_KEY is not set: cp .env.example .env" in result.output


def test_the_readme_demo_flow_with_fake_agents(
    monkeypatch: pytest.MonkeyPatch, dataset: Path, tmp_path: Path
) -> None:
    """run → pending → approve → deny → postings → costs → reset, as in the README, at $0."""
    from invoiceiq.pipeline import graph
    from tests.fakes import fake_batch_auditor, fake_poster, fake_router_extractor, runtime_settings

    settings = runtime_settings(dataset, tmp_path)
    for folder in ("invoices", "ground_truth"):  # the CLI reads these from data_dir
        (settings.data_dir / folder).symlink_to(dataset / folder)
    real_deps = graph.pipeline_deps

    async def fake_deps(s: Settings) -> graph.PipelineDeps:
        return await real_deps(
            s,
            extractor=fake_router_extractor(s, dataset),
            auditor=fake_batch_auditor(s, dataset),
            poster=fake_poster(s),
        )

    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    def no_telemetry(s: Settings, *, console: bool = False) -> None:
        pass

    monkeypatch.setattr(cli, "configure_telemetry", no_telemetry)
    monkeypatch.setattr(cli, "pipeline_deps", fake_deps)
    runner = CliRunner()

    def invoke(*args: str) -> str:
        result = runner.invoke(cli.app, list(args))
        assert result.exit_code == 0, result.output
        return result.output

    out = invoke("run", "inv-001", "inv-024", "inv-025", "--fresh")
    assert "decisions matching ground truth: 3/3" in out
    out = invoke("pending")
    assert "inv-024" in out and "inv-025" in out
    assert "posted" in invoke("approve", "inv-024", "--user", "asha")
    assert "denied" in invoke("deny", "inv-025", "--user", "asha", "--note", "price not agreed")
    out = invoke("postings")
    assert "2 posting(s)" in out and "asha" in out and "policy" in out
    assert "spent $" in invoke("costs")
    invoke("reset")
    assert "no runtime ledger yet" in invoke("postings")
