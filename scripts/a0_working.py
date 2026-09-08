"""2.4 — A0 over the whole 150-task working set. **This spends real quota.**

The measured run. It produces the first number this project reports, so everything about
it is declared before it starts: the task list is `splits/working.json`, the ceilings are
`config/runs/working-set.toml`, and both are inside the run's fingerprint, so a resume
that changed either is refused rather than appended to.

**Never the reserve set.** `splits/reserve.json` is not read here or anywhere else until
the reserve run.

Three things happen, in this order, and the last two spend nothing:

1. The run, under the budget guard and the run ledger. One call per task, `strong` role.
2. The empty-result floor for **this split**, measured by executing the 150 reference
   queries in the sandbox. `docs/EQUIVALENCE.md` records 4.7389% for the 1,034-task frame
   and says that share belongs beside every accuracy figure this project reports; the
   figure beside a 150-task accuracy has to be the 150-task one.
3. The projection into `results/a0-working.json`, derived from the ledger and regenerable
   from it with `--project-only`.

**The keys are not loaded for you**, as `scripts/client_smoke.py` documents. The credential
is preflighted once here rather than discovered by every task separately — which is the
defect this run would otherwise have paid for 150 times.

    set -a && . ./.env && set +a
    uv run python scripts/a0_working.py [--run-id ID] [--limit N] [--project-only]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from query_pilot.agents import A0, ROLE, project, write_results
from query_pilot.client import Client, ClientError
from query_pilot.difficulty import difficulty_of
from query_pilot.run import Run, RunConfig, RunLedger, new_run_id, summarise, write_summary
from query_pilot.sandbox import CopyScope, Sandbox, SubstrateCopies
from query_pilot.tasks import load_tasks

REPO = Path(__file__).resolve().parent.parent
SPIDER = REPO / "data" / "spider"
WORKING = REPO / "splits" / "working.json"
RUN_CONFIG = REPO / "config" / "runs" / "working-set.toml"
RESULTS = REPO / "results" / "a0-working.json"
SPLIT_NAME = "working"


def difficulties() -> dict[str, str]:
    """Spider's own parsed query per task, labelled by this project's rule.

    Differential-tested against canonical `eval_hardness` on all 1,034 frame tasks at 0.3
    with zero disagreements, so this is a lookup rather than a claim.
    """
    dev = json.loads((SPIDER / "dev.json").read_text())
    return {f"dev-{index:04d}": difficulty_of(item["sql"]) for index, item in enumerate(dev)}


def empty_reference_tasks(tasks) -> int:
    """How many of this split's reference queries return no rows.

    Executed through the same sandbox, with the same caps and the same read-only
    connection, that the run itself used — a floor measured by a second mechanism would be
    a floor for a different measurement.
    """
    sandbox = Sandbox()
    copies = SubstrateCopies(SPIDER / "database", scope=CopyScope.RUN)
    empty = 0
    try:
        for task in tasks:
            with copies.for_task(task.db_id, task_id=task.task_id) as database:
                result = sandbox.execute(database, task.reference_sql)
            if not result.ok:
                raise SystemExit(
                    f"{task.task_id}: the reference query does not execute ({result.error}). "
                    f"The frame is every task whose reference executes, so this cannot happen "
                    f"without the substrate having changed underneath splits/frame.json."
                )
            empty += not result.rows
    finally:
        copies.close()
    return empty


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=None, help="resume an existing run instead")
    parser.add_argument("--limit", type=int, default=None, help="fewer tasks; not the measurement")
    parser.add_argument(
        "--project-only",
        action="store_true",
        help="rebuild results/a0-working.json from an existing --run-id, spending nothing",
    )
    options = parser.parse_args()

    if not (SPIDER / "dev.json").exists():
        print("substrate not acquired; see docs/SUBSTRATE.md", file=sys.stderr)
        return 2

    tasks = load_tasks(SPIDER, WORKING)
    if options.limit is not None:
        tasks = tasks[: options.limit]

    if options.project_only:
        if not options.run_id:
            print("--project-only needs --run-id", file=sys.stderr)
            return 2
        ledger_path = RunLedger(options.run_id).path
        document = project(
            ledger_path,
            split=SPLIT_NAME,
            difficulty=difficulties(),
            empty_reference_tasks=empty_reference_tasks(tasks),
        )
        print(f"wrote {write_results(document, RESULTS)}")
        return 0

    run_id = options.run_id or new_run_id()
    config = RunConfig.load(RUN_CONFIG, [task.task_id for task in tasks], run_id=run_id)

    client = Client.from_config()
    try:
        client.registry.candidates(ROLE)
    except ClientError as exc:
        print(f"{exc}\n\nLoad the keys first:  set -a && . ./.env && set +a", file=sys.stderr)
        return 2

    ledger = RunLedger(run_id)
    a0 = A0(client, tasks, SPIDER / "database")
    print(
        f"run {run_id}: {len(tasks)} tasks, agent {config.agent}, role {a0.role}, "
        f"concurrency {config.concurrency}, token ceiling {config.token_ceiling:,}"
    )
    try:
        with ledger:
            report = await Run(config, ledger).execute(a0)
    finally:
        a0.close()

    write_summary(summarise(ledger.path), ledger.path.parent)
    document = project(
        ledger.path,
        split=SPLIT_NAME,
        difficulty=difficulties(),
        empty_reference_tasks=empty_reference_tasks(tasks),
    )
    written = write_results(document, RESULTS)

    print(json.dumps(document["measurement"], indent=2))
    print(json.dumps(document["execution_accuracy"], indent=2))
    print(json.dumps(document["tokens"], indent=2))
    print(json.dumps(document["reasons"], indent=2))
    print(f"\nledger  {ledger.path}\nresults {written}")
    # Non-zero when anything failed, not only when a ceiling stopped the run: a run whose
    # every task failed still reports status "complete", which means "not cut short".
    return 0 if report.complete and report.tasks_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
