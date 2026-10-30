"""Regression gate (spec §17.5): latest eval results vs evals/baseline.json.

    uv run python -m evals.compare            # exit 1 if any metric dropped by more than 3 points
    uv run python -m evals.compare --accept   # make the latest full runs the new baseline

Compares runs/evals/latest-<dataset>-<model>.json. A run over a different number of cases than the
baseline (a --limit run) is skipped, not compared. $0: it only reads files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from evals.common import results_dir
from invoiceiq.config import get_settings

BASELINE = Path(__file__).with_name("baseline.json")
MAX_DROP_POINTS = 3.0
LOWER_IS_BETTER = {"task_failures"}  # counts, not rates: any increase fails


def load_latest() -> dict[str, dict[str, Any]]:
    out = results_dir(get_settings())
    runs = [json.loads(p.read_text()) for p in sorted(out.glob("latest-*.json"))]
    return {f"{r['dataset']}/{r['model'].split(':')[-1]}": r for r in runs}


def regressions(base: dict[str, Any], new: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for metric, old in base["metrics"].items():
        if metric not in new["metrics"]:
            found.append(f"{metric}: missing from the new run")
            continue
        value = new["metrics"][metric]
        if metric in LOWER_IS_BETTER:
            if value > old:
                found.append(f"{metric}: {old:g} -> {value:g}")
        elif (old - value) * 100 > MAX_DROP_POINTS:
            found.append(f"{metric}: {old:.3f} -> {value:.3f} ({(value - old) * 100:+.1f} points)")
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accept", action="store_true", help="save the latest full runs as the baseline")
    args = parser.parse_args()
    latest = load_latest()
    baseline: dict[str, Any] = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}

    if args.accept:
        for key, run in latest.items():
            if key in baseline and run["cases"] < baseline[key]["cases"]:
                base_cases = baseline[key]["cases"]
                print(f"skip {key}: {run['cases']} cases is a partial run (baseline has {base_cases})")
                continue
            baseline[key] = {k: run[k] for k in ("cases", "metrics", "cost_usd", "time")}
            print(f"accepted {key}: {json.dumps(run['metrics'])}")
        BASELINE.write_text(json.dumps(baseline, indent=1, sort_keys=True) + "\n")
        return 0

    failed = False
    for key, base in sorted(baseline.items()):
        run = latest.get(key)
        if run is None:
            print(f"--   {key}: no run yet")
            continue
        if run["cases"] != base["cases"]:
            print(f"--   {key}: {run['cases']} cases vs {base['cases']} in the baseline; not compared")
            continue
        found = regressions(base, run)
        failed |= bool(found)
        print(f"{'FAIL' if found else 'ok  '} {key}" + "".join(f"\n       {f}" for f in found))
    for key in sorted(set(latest) - set(baseline)):
        print(f"new  {key}: not in the baseline (run with --accept to add it)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
