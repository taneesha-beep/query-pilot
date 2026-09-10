"""3.6 — A1 over the whole 150-task working set. **This spends real quota, and a lot of it.**

The measured run, and the second real number this project reports. Everything about it is
declared before it starts: the task list is `splits/working.json`, the ceilings are
`config/runs/a1-working.toml`, and both are inside the run's fingerprint, so a resume that
changed either is refused rather than appended to. It is **the same 150 tasks A0 was measured
on**, on the same model and the same role, because the comparison this run exists to support
is between two agent loops and not between two splits.

**Never the reserve set.** `splits/reserve.json` is not read here or anywhere else until the
reserve run.

**This run is expected to span one or more quota resets, and that is the normal case.** A1's
acceptance run measured 5,284.2 tokens a task, which projects a 150-task run near 790,000
tokens against Groq's published 200,000 a day for one model — roughly four provider-days. The
`strong` role has **no spillover** (`config/providers.toml`), which is what makes that a delay
rather than a contamination: no other model can serve the role, so a day's exhaustion stops
the run instead of quietly finishing it on something else. 1.3's resume rule is what carries
it across: a task recorded `complete` is skipped, a task recorded `failed` is retried, and the
token ceiling is inherited across every session while the wall clock is per session.

    set -a && . ./.env && set +a
    uv run python scripts/a1_working.py --max-requests 30            # first stage
    uv run python scripts/a1_working.py --run-id ID --max-requests 400   # and every resume
    uv run python scripts/a1_working.py --run-id ID --project-only   # spends nothing

**`--max-requests` is how a long run is spent in reviewable stages**, and it is a per-session
backstop rather than a budget: no run declaration carries a request count, because requests
are the unit a free tier refuses in rather than the unit a budget is spent in. Reaching it
stops the run through its **own** guard as `operator`, so tasks that never started are unrun
rather than failed and the next session picks them up. See `run.guard.RequestCeiling`.

**There is no `--limit`, and its absence is deliberate.** The declared task list is inside the
run's fingerprint, so a run started over ten tasks can never be resumed into the full 150 — it
would be a second experiment in one ledger, which is exactly what the fingerprint exists to
refuse. A short first stage is a request ceiling on the real run, not a shorter run.

Three things happen, in this order, and the last two spend nothing:

1. The run, under the budget guard and the run ledger, at concurrency 1 (constraint 64).
2. The empty-result floor for **this split**, measured by executing the 150 reference queries
   in the sandbox — the same figure beside A0's accuracy, measured the same way, because a
   floor beside a 150-task accuracy has to be the 150-task one.
3. The two committed artifacts, both derived from the one ledger and both regenerable from it
   with `--project-only`: the projection into `results/a1-working.json` and 3.4's four
   trajectory metrics into `results/a1-trajectory-metrics.json`. They are written together
   because they are two files carrying overlapping facts about one run, and they agree by
   construction only while they come from the same run directory.

**The keys are not loaded for you**, as `scripts/client_smoke.py` documents. The credential is
preflighted once here rather than discovered by every task separately.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from query_pilot.agents import (
    A1,
    METRICS_NAME,
    ROLE,
    TOOL_SCHEMAS,
    project,
    write_metrics,
    write_results,
)
from query_pilot.client import Client, ClientError
from query_pilot.difficulty import difficulty_of
from query_pilot.run import (
    RequestCeiling,
    Run,
    RunConfig,
    RunLedger,
    new_run_id,
    read_rows,
    summarise,
    write_summary,
)
from query_pilot.sandbox import CopyScope, Sandbox, SubstrateCopies
from query_pilot.tasks import load_tasks

REPO = Path(__file__).resolve().parent.parent
SPIDER = REPO / "data" / "spider"
WORKING = REPO / "splits" / "working.json"
RUN_CONFIG = REPO / "config" / "runs" / "a1-working.toml"
RESULTS = REPO / "results" / "a1-working.json"
METRICS = REPO / "results" / METRICS_NAME
SPLIT_NAME = "working"


def difficulties() -> dict[str, str]:
    """Spider's own parsed query per task, labelled by this project's rule.

    Differential-tested against canonical `eval_hardness` on all 1,034 frame tasks at 0.3
    with zero disagreements, so this is a lookup rather than a claim. Identical to
    `scripts/a0_working.py`'s, because the two runs must be labelled by one rule.
    """
    dev = json.loads((SPIDER / "dev.json").read_text())
    return {f"dev-{index:04d}": difficulty_of(item["sql"]) for index, item in enumerate(dev)}


def empty_reference_tasks(tasks) -> int:
    """How many of this split's reference queries return no rows.

    Executed through the same sandbox, with the same caps and the same read-only connection,
    that the run itself used — a floor measured by a second mechanism would be a floor for a
    different measurement.
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


def artifacts(ledger_path: Path, tasks) -> tuple[Path, Path]:
    """Write both committed files from one run directory. Spends nothing."""
    document = project(
        ledger_path,
        split=SPLIT_NAME,
        difficulty=difficulties(),
        empty_reference_tasks=empty_reference_tasks(tasks),
    )
    written = write_results(document, RESULTS)
    write_metrics(ledger_path.parent, METRICS)
    print(json.dumps(document["measurement"], indent=2))
    print(json.dumps(document["execution_accuracy"], indent=2))
    print(json.dumps(document["tokens"], indent=2))
    print(json.dumps(document["reasons"], indent=2))
    for key in ("terminations", "validation_rules"):
        if key in document:
            print(f"{key}: {json.dumps(document[key])}")
    return written, METRICS


def report_tasks(ledger_path: Path) -> None:
    """One line a trajectory, out of the ledger, so a stage can be read before the next one."""
    solved = 0
    terminations: Counter[str] = Counter()
    for row in read_rows(ledger_path):
        if row["kind"] != "task" or row["status"] != "complete":
            continue
        detail = row["detail"]
        solved += bool(detail.get("solved"))
        terminations[detail["termination"]] += 1
        mark = "solved  " if detail.get("solved") else f"{detail['reason']:<8}"
        print(
            f"  {row['task_id']}  {mark}  {detail['db_id']:<22} "
            f"{detail['termination']:<16} {detail['turns']:>2}t "
            f"{detail['tool_calls']:>2}c  {detail['sql']!r}"
        )
    print(f"\n  complete and solved: {solved}    terminations: {dict(terminations)}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=None, help="resume an existing run instead")
    parser.add_argument(
        "--max-requests",
        type=int,
        default=400,
        help="per-session backstop on provider requests; reaching it stops the run as operator",
    )
    parser.add_argument(
        "--project-only",
        action="store_true",
        help="rebuild both results files from an existing --run-id, spending nothing",
    )
    parser.add_argument(
        "--per-task", action="store_true", help="print one line per completed trajectory"
    )
    options = parser.parse_args()

    if not (SPIDER / "dev.json").exists():
        print("substrate not acquired; see docs/SUBSTRATE.md", file=sys.stderr)
        return 2

    tasks = load_tasks(SPIDER, WORKING)

    if options.project_only:
        if not options.run_id:
            print("--project-only needs --run-id", file=sys.stderr)
            return 2
        ledger_path = RunLedger(options.run_id).path
        if options.per_task:
            report_tasks(ledger_path)
        written, metrics = artifacts(ledger_path, tasks)
        print(f"\nresults {written}\nmetrics {metrics}")
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
    run = Run(config, ledger)
    counted = RequestCeiling(
        client,
        run.guard,
        options.max_requests,
        on_ceiling=lambda n: print(
            f"request ceiling of {n} reached; stopping the run", file=sys.stderr
        ),
    )
    a1 = A1(counted, tasks, SPIDER / "database", ledger.directory)  # type: ignore[arg-type]

    print(
        f"run {run_id}: {len(tasks)} tasks, agent {config.agent}, role {a1.role}, "
        f"{len(TOOL_SCHEMAS)} tools, concurrency {config.concurrency}, "
        f"token ceiling {config.token_ceiling:,}, at most {options.max_requests} requests"
    )
    try:
        with ledger:
            report = await run.execute(a1)
    finally:
        a1.close()

    write_summary(summarise(ledger.path), ledger.path.parent)
    if options.per_task:
        report_tasks(ledger.path)

    print(
        json.dumps(
            {
                "run_id": run_id,
                "status": report.status,
                "incomplete_reason": report.incomplete_reason,
                "tasks_complete": report.tasks_complete,
                "tasks_failed": report.tasks_failed,
                "tasks_remaining_this_session": report.tasks_remaining,
                "requests_this_session": counted.requests,
                "prompt_tokens": report.prompt_tokens,
                "completion_tokens": report.completion_tokens,
                "elapsed_s": round(report.elapsed_s, 2),
                "ledger": str(ledger.path),
            },
            indent=2,
        )
    )

    written, metrics = artifacts(ledger.path, tasks)
    print(f"\nledger  {ledger.path}\nresults {written}\nmetrics {metrics}")
    if not report.complete or report.tasks_failed:
        print(
            f"\nThis run is NOT finished. Resume it:\n"
            f"    uv run python scripts/a1_working.py --run-id {run_id} --max-requests N\n"
            f"No accuracy figure may be reported from a partial run, and the projection "
            f"above says TBD rather than a rate for exactly that reason.",
            file=sys.stderr,
        )
    # Non-zero when anything failed or a ceiling stopped the run, not only on a crash.
    # Constraint 44: a run status of `complete` means "not cut short", never "it worked".
    return 0 if report.complete and report.tasks_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
