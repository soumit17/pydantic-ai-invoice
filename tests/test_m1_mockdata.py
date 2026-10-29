"""M1 acceptance: the generator is deterministic, and the checks + policy reproduce the ground truth."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date
from pathlib import Path

from invoiceiq.models import GroundTruth, RawInvoice
from invoiceiq.pipeline.checks import run_deterministic_checks
from invoiceiq.policy import Policy, to_inr
from invoiceiq.repos.ledger import LedgerRepo
from tests.conftest import generate

AS_OF = date(2026, 10, 29)


def digest(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())}


def load_truth(dataset: Path) -> list[GroundTruth]:
    files = sorted((dataset / "ground_truth").glob("*.json"))
    return [GroundTruth.model_validate_json(p.read_text()) for p in files]


def test_corpus_shape(dataset: Path) -> None:
    truth = load_truth(dataset)
    assert len(truth) == 40
    assert Counter(t.format for t in truth) == {"pdf": 36, "png": 3, "eml": 1}
    assert sum(1 for p in (dataset / "invoices").iterdir()) == 40
    per_category = Counter(t.category for t in truth)
    assert per_category == {
        1: 10,
        2: 6,
        3: 3,
        4: 2,
        5: 2,
        6: 4,
        7: 1,
        8: 1,
        9: 2,
        10: 2,
        11: 3,
        12: 1,
        13: 2,
        14: 1,
    }


def test_regeneration_is_byte_identical(dataset: Path, tmp_path: Path) -> None:
    generate(tmp_path)
    assert digest(tmp_path / "ground_truth") == digest(dataset / "ground_truth")
    assert digest(tmp_path / "invoices") == digest(dataset / "invoices")


async def test_checks_and_policy_reproduce_ground_truth(dataset: Path) -> None:
    ledger = LedgerRepo(dataset / "ledger.sqlite")
    policy = Policy()
    mismatches: list[str] = []
    for t in load_truth(dataset):
        if not isinstance(t.expected, RawInvoice):
            decision = policy.decide([], failure=t.expected)
            got_findings: list[tuple[str, str]] = []
        else:
            findings = await run_deterministic_checks(t.expected, ledger=ledger, policy=policy, today=AS_OF)
            decision = policy.decide(findings, total_inr=to_inr(t.expected.total, t.expected.currency))
            got_findings = sorted((x.kind, x.severity) for x in findings)
        want_findings = sorted((x.kind, x.severity) for x in t.expected_findings)
        if got_findings != want_findings or decision.outcome != t.expected_decision:
            mismatches.append(
                f"{t.file_id} [{t.category_name}]: findings {got_findings} vs {want_findings}, "
                f"decision {decision.outcome} vs {t.expected_decision}"
            )
    assert not mismatches, "\n".join(mismatches)
