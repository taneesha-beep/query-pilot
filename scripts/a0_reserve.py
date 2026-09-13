"""The reserve run: A0, once, over the 150 reserve tasks. **This spends real quota.**

The one figure in this project taken on tasks nothing was tuned against. How it is read was
committed in `docs/RESULTS.md` before the run. Its declaration is
`config/runs/reserve-set.toml`: the working set's, with its own split and ceilings of twice what
A0's working run measured, all inside the run's fingerprint.

**This script is the only reader of `splits/reserve.json`.** It refuses a second run on the
reserve set (`query_pilot.reserve.check_once`): resuming the one run with `--run-id` is part of
that run, and a new run ID would be a second reading. There is no `--limit`, because a partial
run is a reading too.

It does what `scripts/a0_working.py` does, in the same order, and reuses that script's own
difficulty and empty-result-floor functions, so both figures are measured the same way:

1. The run, under the budget guard and the run ledger. One call per task, `strong` role.
2. The empty-result floor for this split, by executing its 150 reference queries in the sandbox.
3. The projection into `results/a0-reserve.json`, with the working figure beside it
   (`query_pilot.reserve.compare`) and the ledger's own summary. `--project-only` rebuilds it
   from the run's ledger, spending nothing.

Run it from the repository root, which is where the ledgers and the guard look. **The keys are
not loaded for you**:

    set -a && . ./.env && set +a
    uv run python scripts/a0_reserve.py [--run-id ID] [--project-only]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from a0_working import SPIDER, difficulties, empty_reference_tasks

from query_pilot import reserve
from query_pilot.agents import A0, ROLE, project, write_results
from query_pilot.client import Client, ClientError
from query_pilot.run import Run, RunConfig, RunLedger, new_run_id, summarise, write_summary
from query_pilot.run.ledger import DEFAULT_RUNS_ROOT
from query_pilot.tasks import load_tasks

REPO = Path(__file__).resolve().parent.parent


def build(ledger: Path, tasks) -> dict[str, Any]:
    """The projection, the working figure beside it, and the ledger's summary."""
    document = project(
        ledger,
        split=reserve.SPLIT,
        difficulty=difficulties(),
        empty_reference_tasks=empty_reference_tasks(tasks),
    )
    working = json.loads((REPO / reserve.WORKING_RESULTS).read_text())
    document["beside_the_working_set"] = reserve.compare(document, working)
    document["ledger_summary"] = summarise(ledger).as_file()
    return document


def report(document: dict[str, Any], written: Path) -> None:
    """Aggregates only. Nothing per task is printed."""
    beside = document["beside_the_working_set"]
    for key in ("reserve", "working", "difference", "empty_result_floor"):
        print(f"{key}: {json.dumps(beside[key])}")
    print(f"tokens: {json.dumps(document['tokens']['total'])}")
    print(f"run: {json.dumps({k: v for k, v in document['run'].items() if k != 'error_classes'})}")
    print(f"\nledger  {document['measurement']['ledger']}\nresults {written}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=None, help="resume the reserve run, never another")
    parser.add_argument(
        "--project-only",
        action="store_true",
        help="rebuild results/a0-reserve.json from the reserve run's ledger, spending nothing",
    )
    options = parser.parse_args()

    if Path.cwd().resolve() != REPO:
        print(
            f"run this from {REPO}: the ledgers and the guard are relative to it", file=sys.stderr
        )
        return 2
    if not (SPIDER / "dev.json").exists():
        print("substrate not acquired; see docs/SUBSTRATE.md", file=sys.stderr)
        return 2
    if options.project_only and not options.run_id:
        print("--project-only needs --run-id", file=sys.stderr)
        return 2
    try:
        reserve.check_once(options.run_id, runs_root=DEFAULT_RUNS_ROOT, results=reserve.RESULTS)
    except reserve.SecondReading as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    if options.project_only:
        tasks = load_tasks(SPIDER, REPO / reserve.SPLIT_FILE)
        document = build(RunLedger(options.run_id).path, tasks)
        report(document, write_results(document, reserve.RESULTS))
        return 0

    # The credential before the split: a start that is going to fail for want of a key must
    # fail before `splits/reserve.json` is opened, not after.
    client = Client.from_config()
    try:
        candidates = client.registry.candidates(ROLE)
    except ClientError as exc:
        print(f"{exc}\n\nLoad the keys first:  set -a && . ./.env && set +a", file=sys.stderr)
        return 2

    tasks = load_tasks(SPIDER, REPO / reserve.SPLIT_FILE)
    run_id = options.run_id or new_run_id()
    config = RunConfig.load(REPO / reserve.RUN_CONFIG, [t.task_id for t in tasks], run_id=run_id)
    ledger = RunLedger(run_id)
    a0 = A0(client, tasks, SPIDER / "database")
    print(
        f"run {run_id}: {len(tasks)} tasks, agent {config.agent}, role {a0.role}, "
        f"pools {', '.join(str(c) for c in candidates)}, concurrency {config.concurrency}, "
        f"token ceiling {config.token_ceiling:,}, wall clock {config.wall_clock_ceiling_s:,.0f} s"
    )
    try:
        with ledger:
            outcome = await Run(config, ledger).execute(a0)
    finally:
        a0.close()

    write_summary(summarise(ledger.path), ledger.path.parent)
    document = build(ledger.path, tasks)
    report(document, write_results(document, reserve.RESULTS))
    # Non-zero when anything failed, not only when a ceiling stopped the run: a run whose
    # every task failed still reports status "complete", which means "not cut short".
    return 0 if outcome.complete and outcome.tasks_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
