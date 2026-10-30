"""Dataset 1, extraction (spec §17.1). LIVE: about $0.04 on Haiku, $0.70 on Sonnet for all 40.

uv run python -m evals.extraction --limit 3     # cheap first try
uv run python -m evals.extraction               # all 40 on Haiku (M8 acceptance: field accuracy ≥ 95%)
uv run python -m evals.extraction --final       # once on Sonnet, for the journal's comparison
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import MaxModelRequests

from evals.common import evaluate, ground_truths, parse_args, setup
from evals.evaluators import (
    CaseMeta,
    DocInput,
    Extracted,
    ExtractionSummary,
    FieldAccuracy,
    KindCorrect,
    LineItemF1,
    SpansCaptured,
)
from invoiceiq.agents.extractor import EXTRACT_LIMITS, ExtractorAgent, build_extractor, extract_deps
from invoiceiq.config import Settings
from invoiceiq.models import GroundTruth
from invoiceiq.pipeline.documents import load_document


def build_dataset(truths: list[GroundTruth]) -> Dataset[DocInput, Extracted, CaseMeta]:
    return Dataset[DocInput, Extracted, CaseMeta](
        name="extraction",
        cases=[
            Case(
                name=t.file_id,
                inputs=DocInput(file_id=t.file_id, file_name=t.file_name),
                expected_output=t.expected,
                metadata=CaseMeta(category=t.category_name),
            )
            for t in truths
        ],
        evaluators=[
            KindCorrect(),
            FieldAccuracy(),
            LineItemF1(),
            # 1 request, plus up to 2 output retries (the extractor's retries={'output': 2}).
            MaxModelRequests(max_requests=3),
            SpansCaptured(),
        ],
        report_evaluators=[ExtractionSummary()],
    )


def extraction_task(agent: ExtractorAgent, settings: Settings) -> Callable[[DocInput], Awaitable[Extracted]]:
    deps = extract_deps(settings)

    async def extract(doc: DocInput) -> Extracted:
        prompt = load_document(settings.data_dir / "invoices" / doc.file_name)
        result = await agent.run(
            prompt, deps=deps, usage_limits=EXTRACT_LIMITS, metadata={"invoice_id": doc.file_id}
        )
        return result.output

    return extract


async def main() -> None:
    args = parse_args(__doc__ or "")
    settings, model, spent = setup(args)
    dataset = build_dataset(ground_truths(settings, args))
    task = extraction_task(build_extractor(settings, model=model), settings)
    await evaluate(dataset, task, settings=settings, model=model, spent_before=spent, args=args)


if __name__ == "__main__":
    asyncio.run(main())
