"""Compute 3.4's four trajectory metrics over a run directory. **Spends no quota.**

Reads a run's `transcripts/` and its `ledger.jsonl` and writes one JSON file. Nothing here
talks to a provider, opens the substrate, or executes SQL: the trajectories already happened
and this only counts what they did.

    uv run python scripts/a1_metrics.py runs/<run_id> [--out results/a1-trajectory-metrics.json]
    uv run python scripts/a1_metrics.py tests/transcripts/built     # the acceptance

**Every definition is written into the output file**, under `definitions`, rather than living
only in `agents/metrics.py`. 3.6 publishes these figures and 4.3 quotes a rate beside them, so
a committed metrics file that did not say what its denominators were would be four numbers
nobody could check.

**The denominators are part of the numbers.** Recovery rate is reported three times — first
`execute_sql` errored, first returned empty, either — because "returned empty" is not
obviously a failure when 49 of the frame's 1,034 references legitimately return no rows.
Where a denominator is 0 the rate is `TBD`, not 0.0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from query_pilot.agents.metrics import METRICS_NAME, compute, write_metrics
from query_pilot.run.ledger import LEDGER_NAME

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path, help="a run directory, or a fixture one")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"where to write the JSON (default: results/{METRICS_NAME})",
    )
    parser.add_argument(
        "--per-task", action="store_true", help="also print one line per trajectory"
    )
    options = parser.parse_args()

    directory = options.run_directory
    if not (directory / LEDGER_NAME).exists():
        print(f"no {LEDGER_NAME} in {directory}", file=sys.stderr)
        return 2

    metrics = compute(directory)
    if not metrics.tasks:
        print(f"no transcripts under {directory}", file=sys.stderr)
        return 2

    if options.per_task:
        for row in metrics.tasks:
            waste = "-" if row.wasted_calls is None else str(row.wasted_calls)
            print(
                f"  {row.task_id:<14} {row.termination!s:<16} "
                f"solved={row.solved!s:<5} {row.turns:>2}t {row.tool_calls:>2}c "
                f"first_query={row.first_execute_sql!s:<6} wasted={waste:<3} "
                f"{'' if row.complete else 'INCOMPLETE '}"
                f"{'' if row.counts_agree else 'COUNTS DISAGREE WITH end '}"
            )

    destination = options.out or REPO / "results" / METRICS_NAME
    payload = write_metrics(directory, destination)
    print(json.dumps({k: v for k, v in payload.items() if k != "definitions"}, indent=2))
    print(f"\nwritten to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
