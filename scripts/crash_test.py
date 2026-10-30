"""Crash test (spec §11.3): kill -9 a durable batch worker mid-run, restart it, and check the books.

    uv run python scripts/crash_test.py                       # fake agents (FunctionModel): $0
    uv run python scripts/crash_test.py --live                # real Haiku: ~$0.07 for 40 invoices
    uv run python scripts/crash_test.py --n 12 --kill-after 5 # smaller and faster

Every run works on its own copy of data/ under runs/crash-<time>/, so data/runtime/ is never touched.
Checks after the restart:
  1. every invoice finished (none failed, none left processing)
  2. the ledger has exactly one posting per posted invoice, and no idempotency key twice
  3. no re-spend: invoices finished before the kill made no model calls after it
     (fake: the fakes' call log; live: the cost ledger's per-invoice lines)
  4. fake mode only: every decision matches the ground truth
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
TERMINAL = ("posted", "awaiting_approval", "held", "rejected", "denied", "failed")
BATCH_ID = "crash-batch"


# --- the worker (a separate process, so it can be killed) -----------------------------------------


async def worker(root: Path, n: int, fake: bool) -> None:
    from invoiceiq.config import get_settings
    from invoiceiq.pipeline.batch import start_runtime, stop_runtime, submit_batch

    settings = get_settings()  # DATA_DIR / RUNS_DIR come from the environment the parent set
    if fake:
        from pydantic_ai import models

        from tests.fakes import fake_batch_auditor, fake_poster, fake_router_extractor

        models.ALLOW_MODEL_REQUESTS = False
        log = root / "calls.jsonl"
        data = settings.data_dir
        await start_runtime(
            settings,
            extractor=fake_router_extractor(settings, data, durable=True, delay=0.25, log=log),
            auditor=fake_batch_auditor(settings, data, durable=True, delay=0.15, log=log),
            escalation_auditor=fake_batch_auditor(settings, data, durable=True, thinking="medium", log=log),
            poster=fake_poster(settings),
        )
    else:
        from invoiceiq.telemetry import configure_telemetry

        configure_telemetry(settings)
        await start_runtime(settings)  # durable Haiku agents; launch() recovers a crashed batch
    files = sorted((settings.data_dir / "invoices").iterdir())[:n]
    handle = await submit_batch(BATCH_ID, [(p.stem, p) for p in files])  # attaches if it already exists
    results = await handle.get_result()
    print("RESULT " + json.dumps(results), flush=True)
    stop_runtime()


# --- the parent: start, kill, restart, verify -----------------------------------------------------


def records(root: Path) -> dict[str, dict[str, Any]]:
    db_path = root / "data" / "runtime" / "app.sqlite"
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(db_path) as db:
            rows = db.execute("SELECT invoice_id, status, record_json FROM invoices").fetchall()
    except sqlite3.OperationalError:  # table not created yet, or briefly locked
        return {}
    return {i: {"status": s, **json.loads(r)} for i, s, r in rows if s in TERMINAL}


def completed_workflows(root: Path) -> set[str]:
    """Invoices whose child workflow DBOS has marked SUCCESS: finished *and* checkpointed.

    The stored record isn't enough: auto_post saves "posted" inside its step, so a kill between that
    save and the step's checkpoint leaves a posted record whose step (and poster calls) will re-run.
    """
    with sqlite3.connect(root / "data" / "runtime" / "dbos.sqlite") as db:
        rows = db.execute(
            "SELECT workflow_uuid FROM workflow_status WHERE status = 'SUCCESS' AND workflow_uuid LIKE ?",
            (f"{BATCH_ID}:%",),
        ).fetchall()
    return {uuid.split(":", 1)[1] for (uuid,) in rows}


def start(root: Path, n: int, fake: bool, env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    cmd = [sys.executable, __file__, "worker", str(root), "--n", str(n)] + (["--fake"] if fake else [])
    return subprocess.Popen(cmd, cwd=PROJECT, env=env, stdout=log.open("wb"), stderr=subprocess.STDOUT)


def main() -> int:  # noqa: C901
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="real model calls (costs money)")
    parser.add_argument("--n", type=int, default=40)
    parser.add_argument("--kill-after", type=int, default=15)
    parser.add_argument("--root", type=Path, help="work directory (default: runs/crash-<time>)")
    args = parser.parse_args()
    fake = not args.live

    root = args.root or PROJECT / "runs" / f"crash-{datetime.now():%Y%m%d-%H%M%S}"
    (root / "data").mkdir(parents=True)
    for name in ("ledger.sqlite",):
        shutil.copyfile(PROJECT / "data" / name, root / "data" / name)
    for folder in ("invoices", "ground_truth"):
        shutil.copytree(PROJECT / "data" / folder, root / "data" / folder)
    costs = (root / "runs" if fake else PROJECT / "runs") / "costs.jsonl"
    env = {
        **os.environ,
        "DATA_DIR": str(root / "data"),
        "RUNS_DIR": str(costs.parent),  # live: the project ledger, so the spend cap still applies
        "AS_OF_DATE": "2026-10-29",
        "PYTHONPATH": str(PROJECT),
        "PYDANTIC_AI_NO_BANNER": "1",
    }
    if fake:
        env["ANTHROPIC_API_KEY"] = "test-dummy"
    print(
        f"{'fake' if fake else 'LIVE'} crash test: {args.n} invoices, kill -9 after {args.kill_after}; "
        f"in {root}"
    )

    # 1. Start, and kill -9 once enough invoices have finished.
    t0 = time.monotonic()
    first = start(root, args.n, fake, env, root / "worker1.log")
    while len(records(root)) < args.kill_after:
        if first.poll() is not None:
            print(f"worker 1 exited before the kill (code {first.returncode}); see {root / 'worker1.log'}")
            return 1
        time.sleep(0.05)
    first.kill()  # SIGKILL: no cleanup, no flush; exactly what DBOS has to survive
    first.wait()
    stored_before = set(records(root))
    done_before = completed_workflows(root)  # the re-spend check is about these
    cost_lines_at_kill = len(costs.read_text().splitlines()) if costs.exists() else 0
    print(f"  killed worker 1 (pid {first.pid}) after {time.monotonic() - t0:.1f}s: "
          f"{len(done_before)} invoices finished, {len(stored_before - done_before)} more stored "
          f"but still in flight {sorted(stored_before - done_before) or ''}")  # fmt: skip

    # 2. Restart: DBOS.launch() recovers the batch.
    second = start(root, args.n, fake, env, root / "worker2.log")
    try:
        second.wait(timeout=900)
    except subprocess.TimeoutExpired:
        second.kill()
        print("worker 2 timed out")
        return 1
    if second.returncode != 0:
        print(f"worker 2 failed (code {second.returncode}); see {root / 'worker2.log'}")
        return 1
    print(f"  worker 2 (pid {second.pid}) finished the batch after {time.monotonic() - t0:.1f}s")

    # 3. Check the books.
    failures: list[str] = []
    final = records(root)
    gt_dir = root / "data" / "ground_truth"
    ids = [p.stem for p in sorted((root / "data" / "invoices").iterdir())[: args.n]]
    missing = [i for i in ids if i not in final]
    failed = [i for i in ids if final.get(i, {}).get("status") == "failed"]
    if missing or failed:
        failures.append(f"not finished: {missing}; failed: {failed}")
    with sqlite3.connect(root / "data" / "runtime" / "ledger.sqlite") as db:
        postings = db.execute(
            "SELECT idempotency_key FROM posted_invoices WHERE posted_by != 'seed'"
        ).fetchall()
    keys = Counter(k for (k,) in postings)
    posted = [i for i in ids if final.get(i, {}).get("status") == "posted"]
    if dupes := [k for k, c in keys.items() if c > 1]:
        failures.append(f"duplicate idempotency keys: {dupes}")
    if len(postings) != len(posted):
        failures.append(f"{len(postings)} ledger postings for {len(posted)} posted invoices")

    expected = {i: json.loads((gt_dir / f"{i}.json").read_text())["expected_decision"] for i in ids}
    wrong = {i: (final[i].get("decision") or {}).get("outcome") for i in ids if i in final
             and (final[i].get("decision") or {}).get("outcome") != expected[i]}  # fmt: skip
    if fake and wrong:
        failures.append(f"decisions differ from ground truth: {wrong}")

    if fake:
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
        pids = {c["pid"] for c in calls}
        second_pid_calls = [c for c in calls if c["pid"] == second.pid]
        respent = sorted({c["invoice"] for c in second_pid_calls if c["invoice"] in done_before})
        repeated = Counter((c["agent"], c["invoice"]) for c in calls)
        in_flight = sorted({inv for (agent, inv), count in repeated.items() if count > 1})
        print(f"  model calls: {len(calls)} across {len(pids)} worker processes; "
              f"repeated only for the in-flight invoices {in_flight or '(none)'}")  # fmt: skip
    else:
        lines = [json.loads(line) for line in costs.read_text().splitlines()[cost_lines_at_kill:]]
        respent = sorted({ln["invoice"] for ln in lines if ln.get("invoice") in done_before})
        spend_after = sum(float(ln["usd"]) for ln in lines)
        print(f"  {len(lines)} model requests after the kill, ${spend_after:.4f}")
    if respent:
        failures.append(f"re-spend on invoices finished before the kill: {respent}")

    print(f"  invoices finished: {sum(i in final for i in ids)}/{len(ids)}; posted: {len(posted)}; "
          f"ledger postings: {len(postings)}; "
          f"duplicate keys: {sum(c > 1 for c in keys.values())}")  # fmt: skip
    print(f"  decisions matching ground truth: {len(ids) - len(wrong)}/{len(ids)}"
          + (f"  (live auditor differs on {sorted(wrong)})" if wrong and not fake else ""))  # fmt: skip
    if failures:
        print("FAIL\n  " + "\n  ".join(failures))
        return 1
    print("PASS: the batch survived kill -9 with no lost invoices, no double posting and no re-spend")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        wp = argparse.ArgumentParser()
        wp.add_argument("cmd")
        wp.add_argument("root", type=Path)
        wp.add_argument("--n", type=int, default=40)
        wp.add_argument("--fake", action="store_true")
        a = wp.parse_args()
        asyncio.run(worker(a.root, a.n, a.fake))
    else:
        sys.exit(main())
