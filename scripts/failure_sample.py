"""2.5 — draw the failures to read by hand, under the protocol fixed before the run.

**The seed and the rule were fixed on 2026-09-08, before 2.4's run started** and therefore
before this project's first accuracy figure existed. That is the whole point: how many
failures to read and how to choose them decide what the counts mean, and choosing either
after seeing the counts is how a taxonomy is talked into a more comfortable shape.
`docs/FAILURES.md` states the protocol in full above its counts.

The population is every task the run recorded **complete** whose `solved` is false —
`no_sql` and `candidate_error` included. Tasks recorded **failed** have no model answer to
read and are excluded; their count is reported separately.

Thirty of them, drawn at random from the population sorted by task ID. Fewer than thirty in
the population means all of them are read and the counts are reported out of that number.
Nothing tops the sample up, and `splits/reserve.json` is never read here or anywhere else.

    uv run python scripts/failure_sample.py --run-id <id> [--out docs/failure-sample.json]
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from query_pilot.run import RunLedger, read_rows

REPO = Path(__file__).resolve().parent.parent

#: Fixed before the run. Changing it after seeing a sample would be choosing the sample.
SEED = 20260908

#: How many failures are read by hand. The roadmap's figure, and the denominator of every
#: count in docs/FAILURES.md.
SAMPLE_SIZE = 30

#: Tasks whose output was read during 2.3's ten-task acceptance run, before this protocol
#: was fixed. They stay in the population and are categorised like everything else —
#: excluding them would remove exactly the tasks that motivate the category that matters —
#: and they are named so a reader can discount them.
SEEN_BEFORE = ("dev-0126", "dev-0388", "dev-0489")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out", type=Path, default=REPO / "docs" / "failure-sample.json")
    options = parser.parse_args()

    ledger = RunLedger(options.run_id).path
    complete: list[dict] = []
    failed: list[str] = []
    for row in read_rows(ledger):
        if row.get("kind") != "task":
            continue
        if row.get("status") != "complete":
            failed.append(str(row.get("task_id")))
            continue
        complete.append(row)

    population = sorted(
        (row for row in complete if not (row.get("detail") or {}).get("solved")),
        key=lambda row: str(row["task_id"]),
    )
    ids = [str(row["task_id"]) for row in population]
    if len(ids) <= SAMPLE_SIZE:
        drawn, rule = ids, f"all {len(ids)}: the population is not larger than {SAMPLE_SIZE}"
    else:
        drawn = sorted(random.Random(SEED).sample(ids, SAMPLE_SIZE))
        rule = f"random.Random({SEED}).sample(sorted(population), {SAMPLE_SIZE})"

    by_id = {str(row["task_id"]): row for row in population}
    document = {
        "protocol": {
            "fixed_on": "2026-09-08, before the run of 2.4 started",
            "population": "tasks recorded complete whose comparison did not solve",
            "excluded": "tasks recorded failed: no model answer to read",
            "seed": SEED,
            "sample_size": SAMPLE_SIZE,
            "how_drawn": rule,
            "seen_before_the_protocol_was_fixed": list(SEEN_BEFORE),
            "note": "Counts out of the sample. Never re-weighted into a rate.",
        },
        "run": {
            "run_id": options.run_id,
            "ledger": str(ledger),
            "tasks_complete": len(complete),
            "tasks_failed": len(failed),
            "failed_task_ids": failed,
            "non_solves": len(ids),
        },
        "drawn": [
            {
                "task_id": task_id,
                "reason": (by_id[task_id].get("detail") or {}).get("reason"),
                "db_id": (by_id[task_id].get("detail") or {}).get("db_id"),
                "seen_before": task_id in SEEN_BEFORE,
            }
            for task_id in drawn
        ],
    }
    options.out.write_text(json.dumps(document, indent=2) + "\n")
    print(json.dumps(document["protocol"] | document["run"], indent=2))
    print(f"\ndrew {len(drawn)} of {len(ids)} non-solves -> {options.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
