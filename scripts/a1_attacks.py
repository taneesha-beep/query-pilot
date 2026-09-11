"""4.3 — A1 against the 45-case prompt-injection corpus. **This spends real quota.**

Every case in `attacks/corpus.json` becomes one task: its question, its reference query, and
its own database, built from the committed specification into `data/attacks-named/` under the
**neutral name** the agent is shown (`agents.attack_results.database_name` — `staff_1`, not
`atk-0001`). The labels are constraint 80's and are frozen: this script reads them and never
writes one.

**Never the reserve set, and never Spider.** The corpus is its own substrate; nothing here
reads `splits/` or `data/spider`.

A1 exactly as 3.6 ran it: `LocalTools` (constraint 65), the four limits unchanged (constraint
72), concurrency 1 (constraint 64), the `strong` role. `CopyScope.RUN`: each case is its own
database, so no case can see another's even through a shared copy, every connection is
read-only, and control 4 refuses DDL, DML and ATTACH before a connection is opened at all.

    set -a && . ./.env && set +a
    uv run python scripts/a1_attacks.py --max-requests 30                 # first stage
    uv run python scripts/a1_attacks.py --run-id ID --max-requests 300    # and every resume
    uv run python scripts/a1_attacks.py --run-id ID --project-only        # spends nothing

**`--max-requests` stages the run**, as it did for 3.6: reaching it stops the run through its
own guard as `operator`, so cases that never started are unrun rather than failed and the next
session picks them up. There is no `--limit`, for 3.6's reason — the declared task list is in
the run's fingerprint.

Two things happen and the second spends nothing: the run, then `results/attacks.json`, derived
from the run directory's ledger and transcripts and regenerable with `--project-only`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from query_pilot.agents import A1, ROLE, TOOL_SCHEMAS
from query_pilot.agents.attack_results import database_name, write_attack_results
from query_pilot.attacks import build_database, load_corpus
from query_pilot.client import Client, ClientError
from query_pilot.run import (
    RequestCeiling,
    Run,
    RunConfig,
    RunLedger,
    new_run_id,
    summarise,
    write_summary,
)
from query_pilot.sandbox import CopyScope, database_path
from query_pilot.tasks import Task

REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "attacks" / "corpus.json"
DATABASES = REPO / "data" / "attacks-named"
RUN_CONFIG = REPO / "config" / "runs" / "a1-attacks.toml"
RESULTS = REPO / "results" / "attacks.json"


def materialise(corpus) -> tuple[Task, ...]:
    """Build every case's database under its neutral name, and the task that asks of it.

    Rebuilt on every invocation, which is cheap and deterministic: the database a resumed
    session runs against is then provably the one the committed specification describes.
    """
    names = [database_name(case) for case in corpus]
    if len(set(names)) != len(names):
        raise SystemExit(f"two cases share a database name: {sorted(names)}")
    tasks = []
    for case, name in zip(corpus, names, strict=True):
        build_database(case.database, database_path(DATABASES, name))
        tasks.append(
            Task(
                task_id=case.case_id,
                db_id=name,
                question=case.question,
                reference_sql=case.reference_sql,
            )
        )
    return tuple(tasks)


def report(document: dict) -> None:
    """The three figures and one line a case, so a stage can be read before the next one."""
    for key in ("compliance", "containment", "task_damage"):
        figure = document[key]
        print(f"  {key:<12} {figure['count']} of {figure['of']}  ({figure['percent']})")
    print(f"  exposure     {json.dumps(document['beside']['exposure'])}")
    print(f"  run          {json.dumps(document['run'])}")
    for case in document["cases"]:
        if case["status"] is None:
            continue
        surfaces = ",".join(sorted({a["surface"] for a in case["attempts"]})) or "-"
        outcomes = ",".join(a["outcome"] for a in case["attempts"]) or "-"
        print(
            f"  {case['case_id']}  {case['database_shown'] or '-':<10} "
            f"{case['placement']:<10} {case['category']:<15} "
            f"seen={','.join(case['exposed_via']) or '-':<28} "
            f"complied={surfaces:<20} {outcomes:<28} "
            f"{'solved' if case['solved'] else case['reason']!s:<14} {case['termination']}"
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=None, help="resume an existing run instead")
    parser.add_argument(
        "--max-requests",
        type=int,
        default=30,
        help="per-session backstop on provider requests; reaching it stops the run as operator",
    )
    parser.add_argument(
        "--project-only",
        action="store_true",
        help="rebuild results/attacks.json from an existing --run-id, spending nothing",
    )
    options = parser.parse_args()

    corpus = load_corpus(CORPUS)

    if options.project_only:
        if not options.run_id:
            print("--project-only needs --run-id", file=sys.stderr)
            return 2
        document = write_attack_results(RunLedger(options.run_id).directory, corpus, RESULTS)
        report(document)
        print(f"\nresults {RESULTS}")
        return 0

    tasks = materialise(corpus)
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
    a1 = A1(
        counted,  # type: ignore[arg-type]
        tasks,
        DATABASES,
        ledger.directory,
        copy_scope=CopyScope.RUN,
    )

    print(
        f"run {run_id}: {len(tasks)} attack cases, agent {config.agent}, role {a1.role}, "
        f"{len(TOOL_SCHEMAS)} tools, concurrency {config.concurrency}, "
        f"token ceiling {config.token_ceiling:,}, at most {options.max_requests} requests"
    )
    try:
        with ledger:
            outcome = await run.execute(a1)
    finally:
        a1.close()

    write_summary(summarise(ledger.path), ledger.path.parent)
    document = write_attack_results(ledger.directory, corpus, RESULTS)
    report(document)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "status": outcome.status,
                "incomplete_reason": outcome.incomplete_reason,
                "tasks_complete": outcome.tasks_complete,
                "tasks_failed": outcome.tasks_failed,
                "tasks_remaining_this_session": outcome.tasks_remaining,
                "requests_this_session": counted.requests,
                "prompt_tokens": outcome.prompt_tokens,
                "completion_tokens": outcome.completion_tokens,
                "elapsed_s": round(outcome.elapsed_s, 2),
                "ledger": str(ledger.path),
            },
            indent=2,
        )
    )
    print(f"\nledger  {ledger.path}\nresults {RESULTS}")
    if not outcome.complete or outcome.tasks_failed:
        print(
            f"\nThis run is NOT finished. Resume it:\n"
            f"    uv run python scripts/a1_attacks.py --run-id {run_id} --max-requests N\n"
            f"No rate may be reported from a partial run, and the file above says TBD for "
            f"exactly that reason.",
            file=sys.stderr,
        )
    # Constraint 44: a run status of `complete` means "not cut short", never "it worked".
    return 0 if outcome.complete and outcome.tasks_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
