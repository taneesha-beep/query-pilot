"""A2, the cascade, composed and written to `results/a2-working.json`. **Spends nothing.**

5.2's third point on the frontier. It reads three committed files — the always-cheap run's
projection, 3.6's, and the frozen rule's decisions on the cheap run — plus the two gitignored
ledgers for what the projections do not carry (tokens spent before a retry, refused requests),
and composes them through `agents/cascade.py`. The composition, the cost basis and the verdict
were fixed in writing before the cheap run was complete; see that module's docstring.

    uv run python scripts/a2_cheap.py --split working --run-id RUN --project-only
    uv run python scripts/escalation_table.py --run-id RUN \\
        --out docs/escalation-a2-cheap-working.json
    uv run python scripts/cascade.py

The split's empty-reference tasks are found through the sandbox, exactly as every run script
counts them for its floor, so this needs the substrate; the verified wrong references are read
from `docs/a1-failure-counts.json` (4.4). ``tests/test_cascade.py`` regenerates the written file
from committed inputs alone.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from query_pilot.agents import write_results
from query_pilot.agents.cascade import (
    Side,
    attempts_by_task,
    compose,
    earlier_attempts,
    refused_requests,
)
from query_pilot.sandbox import CopyScope, Sandbox, SubstrateCopies
from query_pilot.tasks import load_tasks

REPO = Path(__file__).resolve().parent.parent
SPIDER = REPO / "data" / "spider"
SOURCES = {
    "cheap": "results/a2-cheap-working.json",
    "strong": "results/a1-working.json",
    "escalation": "docs/escalation-a2-cheap-working.json",
}
FAILURE_COUNTS = REPO / "docs" / "a1-failure-counts.json"


def empty_reference_task_ids(tasks) -> list[str]:
    """The split's tasks whose reference query returns no rows, through the run's sandbox.

    The same measurement `scripts/a2_cheap.py` and 3.6's script count for the floor; this one
    keeps the IDs, because the cascade lists what happened on each of those rows.
    """
    sandbox = Sandbox()
    copies = SubstrateCopies(SPIDER / "database", scope=CopyScope.RUN)
    empty: list[str] = []
    try:
        for task in tasks:
            with copies.for_task(task.db_id, task_id=task.task_id) as database:
                result = sandbox.execute(database, task.reference_sql)
            if not result.ok:
                raise SystemExit(f"{task.task_id}: the reference query does not execute")
            if not result.rows:
                empty.append(task.task_id)
    finally:
        copies.close()
    return empty


def side(results: str) -> Side:
    projection = json.loads((REPO / results).read_text())
    ledger = REPO / projection["measurement"]["ledger"]
    if not ledger.exists():
        raise SystemExit(f"no ledger at {ledger}; the earlier attempts cannot be attributed")
    return Side(
        projection=projection,
        earlier=earlier_attempts(projection, attempts_by_task(ledger)),
        refused=refused_requests(ledger),
    )


def main() -> int:
    if not (SPIDER / "dev.json").exists():
        print("substrate not acquired; see docs/SUBSTRATE.md", file=sys.stderr)
        return 2
    for path in SOURCES.values():
        if not (REPO / path).exists():
            print(f"missing {path}; see this script's docstring for the order", file=sys.stderr)
            return 2
    tasks = load_tasks(SPIDER, REPO / "splits" / "working.json")
    wrong = [
        row["task_id"] for row in json.loads(FAILURE_COUNTS.read_text())["reference_verification"]
    ]
    document = compose(
        side(SOURCES["cheap"]),
        side(SOURCES["strong"]),
        json.loads((REPO / SOURCES["escalation"]).read_text()),
        sources=SOURCES,
        empty_reference=empty_reference_task_ids(tasks),
        wrong_reference=wrong,
    )
    written = write_results(document, REPO / "results")
    for key in ("execution_accuracy", "escalation", "routes", "paired", "tokens", "frontier"):
        print(f"{key}: {json.dumps(document[key], indent=2)}")
    print(f"\nwrote {written.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
