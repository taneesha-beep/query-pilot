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

**4.4 added `--census`, for A1, and the defaults are 2.5's unchanged.** A1's run has 34
non-solves, so 2.5's rule taken literally would draw 30 and leave 4 unread. Decided with the
author on 2026-09-11, before any A1 non-solve was read for 4.4: read **all 34** — a census costs
four more tasks than the draw and leaves nothing to choose. `--seen-before` names the tasks
already read before that decision, and `--fixed-on` says when it was made.

    uv run python scripts/failure_sample.py --run-id 20260910-024454-1f69bc --census \\
        --out docs/failure-sample-a1.json --fixed-on "..." --seen-before dev-0186

**A task's last ledger row stands**, as it does everywhere else: a task that failed and was
completed on resume is complete, and is not also counted failed. 2.5's run had no retries, so
its committed draw is what this produces either way.
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
    parser.add_argument(
        "--census", action="store_true", help="read every non-solve rather than drawing"
    )
    parser.add_argument("--fixed-on", default="2026-09-08, before the run of 2.4 started")
    parser.add_argument("--seen-before", nargs="*", default=list(SEEN_BEFORE))
    options = parser.parse_args()
    seen_before = tuple(options.seen_before)

    ledger = RunLedger(options.run_id).path
    last: dict[str, dict] = {}
    for row in read_rows(ledger):
        if row.get("kind") == "task":
            last[str(row.get("task_id"))] = row
    complete = [row for row in last.values() if row.get("status") == "complete"]
    failed = [task_id for task_id, row in last.items() if row.get("status") != "complete"]

    population = sorted(
        (row for row in complete if not (row.get("detail") or {}).get("solved")),
        key=lambda row: str(row["task_id"]),
    )
    ids = [str(row["task_id"]) for row in population]
    if options.census:
        drawn, rule = ids, f"census: all {len(ids)} are read and nothing is drawn"
        sample_size, seed, note = len(ids), None, "Counts out of the population it names."
    elif len(ids) <= SAMPLE_SIZE:
        drawn, rule = ids, f"all {len(ids)}: the population is not larger than {SAMPLE_SIZE}"
        sample_size, seed = SAMPLE_SIZE, SEED
        note = "Counts out of the sample. Never re-weighted into a rate."
    else:
        drawn = sorted(random.Random(SEED).sample(ids, SAMPLE_SIZE))
        rule = f"random.Random({SEED}).sample(sorted(population), {SAMPLE_SIZE})"
        sample_size, seed = SAMPLE_SIZE, SEED
        note = "Counts out of the sample. Never re-weighted into a rate."

    by_id = {str(row["task_id"]): row for row in population}
    document = {
        "protocol": {
            "fixed_on": options.fixed_on,
            "population": "tasks recorded complete whose comparison did not solve",
            "excluded": "tasks recorded failed: no model answer to read",
            "seed": seed,
            "sample_size": sample_size,
            "how_drawn": rule,
            "seen_before_the_protocol_was_fixed": list(seen_before),
            "note": note,
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
                "seen_before": task_id in seen_before,
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
