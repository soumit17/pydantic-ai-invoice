"""Run one approval phase in its own process, so the restart test crosses a real process boundary.

    python -m tests.restart_helper park    <dataset> <root>   # pipeline → pending approval
    python -m tests.restart_helper approve <dataset> <root>   # brand-new process approves it

Prints one JSON line with the outcome. Uses only fake models, so it costs $0.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy")
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic_ai import models  # noqa: E402

from invoiceiq.approvals import resolve  # noqa: E402
from invoiceiq.pipeline.graph import InvoiceSource, pipeline_deps, run_pipeline  # noqa: E402
from tests.fakes import fake_auditor, fake_extractor, fake_poster, runtime_settings, truth  # noqa: E402

models.ALLOW_MODEL_REQUESTS = False
FILE_ID = "inv-017"  # clean but above ₹50k: needs one human approval


async def main(phase: str, dataset: Path, root: Path) -> None:
    settings = runtime_settings(dataset, root)  # copies the seed; the runtime copy is left alone if it exists
    gt = truth(dataset, FILE_ID)
    deps = await pipeline_deps(
        settings, extractor=fake_extractor(settings, gt.expected), auditor=fake_auditor(settings),
        poster=fake_poster(settings),
    )  # fmt: skip
    if phase == "park":
        record = await run_pipeline(InvoiceSource(FILE_ID, dataset / "invoices" / gt.file_name), deps)
        print(json.dumps({"pid": os.getpid(), "status": record.status}))
    else:
        r = await resolve(
            FILE_ID, approve=True, user="alice", poster=deps.poster, store=deps.store, ledger=deps.ledger
        )
        print(json.dumps({"pid": os.getpid(), "status": r.status, "already": r.already_resolved}))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])))
