"""Run A0 end to end on ten tasks. **This spends real quota.**

2.3's acceptance. It is a script and not a test for one reason: no test in this project
makes a live API call, and the suite has to keep passing with no network and no keys.
Everything A0 does is covered by `tests/test_agents.py` against a stub client; what this
adds is the one thing a stub cannot — whether a real model, asked this prompt, answers in a
shape the extractor recognises.

**The smoke set, never the reserve set, and no number taken here is reported.**
`splits/smoke.json` is drawn from within the working set precisely so that debugging costs
15 tasks of a day's quota instead of 150. Ten of them is the acceptance.

Held under the same run ledger, budget guard and declared ceilings as any other run —
`config/runs/smoke-set.toml` — so a run that goes wrong stops rather than degrading, and so
the resume path is exercised before 2.4 has to rely on it.

**The keys are not loaded for you**, the same as `scripts/client_smoke.py`. Without them
this fails before it reaches a provider, so there is a preflight check below that says which
variable is missing and stops — rather than letting the run discover it ten identical times.

    set -a && . ./.env && set +a
    uv run python scripts/a0_smoke.py [--limit 10] [--run-id ID]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from query_pilot.agents import A0, ROLE
from query_pilot.client import Client, ClientError
from query_pilot.run import Run, RunConfig, RunLedger, new_run_id, read_rows
from query_pilot.tasks import load_tasks

REPO = Path(__file__).resolve().parent.parent
SPIDER = REPO / "data" / "spider"
SMOKE = REPO / "splits" / "smoke.json"
RUN_CONFIG = REPO / "config" / "runs" / "smoke-set.toml"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="how many smoke tasks to run")
    parser.add_argument("--run-id", default=None, help="resume an existing run instead")
    options = parser.parse_args()

    if not (SPIDER / "dev.json").exists():
        print("substrate not acquired; see docs/SUBSTRATE.md", file=sys.stderr)
        return 2

    tasks = load_tasks(SPIDER, SMOKE)[: options.limit]
    run_id = options.run_id or new_run_id()
    config = RunConfig.load(RUN_CONFIG, [task.task_id for task in tasks], run_id=run_id)

    client = Client.from_config()

    # Preflight. A missing key is a fact about the environment, and finding it out once
    # here is worth ten tasks finding it out separately — which is what happened the first
    # time this was run, and is the same shape as the defect it exposed in the run loop.
    try:
        client.registry.candidates(ROLE)
    except ClientError as exc:
        print(f"{exc}\n\nLoad the keys first:  set -a && . ./.env && set +a", file=sys.stderr)
        return 2

    ledger = RunLedger(run_id)
    a0 = A0(client, tasks, SPIDER / "database")

    print(f"run {run_id}: {len(tasks)} tasks, agent {config.agent}, role {a0.role}")
    try:
        with ledger:
            report = await Run(config, ledger).execute(a0)
    finally:
        a0.close()

    solved = 0
    for row in read_rows(ledger.path):
        if row["kind"] != "task" or row["status"] != "complete":
            continue
        detail = row["detail"]
        solved += bool(detail.get("solved"))
        mark = "solved  " if detail.get("solved") else f"{detail['reason']:<8}"
        print(f"  {row['task_id']}  {mark}  {detail['db_id']:<22} {detail['sql']!r}")
        if not detail.get("solved"):
            print(f"      reference: {detail['reference_sql']!r}")
            if detail.get("detail"):
                print(f"      {detail['detail']}")

    print(
        json.dumps(
            {
                "run_id": run_id,
                "status": report.status,
                "tasks_complete": report.tasks_complete,
                "tasks_failed": report.tasks_failed,
                "solved": solved,
                "prompt_tokens": report.prompt_tokens,
                "completion_tokens": report.completion_tokens,
                "elapsed_s": round(report.elapsed_s, 2),
                "ledger": str(ledger.path),
            },
            indent=2,
        )
    )
    print(
        "\nThis is an acceptance run on the smoke set. The solved count above is NOT a "
        "result and is not reported anywhere: 2.4 is the measured run."
    )
    # Non-zero when anything failed, not only when a ceiling stopped the run. A run whose
    # every task failed still reports status "complete" — that word means "not cut short",
    # not "it worked" — and an acceptance script that exited 0 on that would be lying.
    return 0 if report.complete and report.tasks_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
