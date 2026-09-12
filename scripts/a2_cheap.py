"""A2-cheap — A1's loop on the cheap model. **This spends real quota.**

One script, two declared runs, differing only in their task list:

- ``--split smoke`` — **5.1's preflight**, the 15 tasks of `splits/smoke.json`, declared in
  `config/runs/a2-cheap-smoke.toml`. It records how often each clause of the escalation rule
  fires on the cheap model before the rule is frozen. **No accuracy figure is taken from it.**
- ``--split working`` — **5.2's always-cheap run**, the 150 tasks of `splits/working.json`,
  declared in `config/runs/a2-cheap-working.toml` with exactly the ceilings 3.6's carries.

**A2-cheap is A1, unchanged, on another role.** The same four limits (constraint 72), prompt,
tools, sandbox, equivalence rule and scoring; the one thing that moves is the model, which is
what makes this and 3.6's strong run two points on one frontier. The role is read from the run
declaration's params — inside the fingerprint, so a resume on a different role is refused — and
it is ``cheap-no-spillover``, an endpoint with nowhere to spill: a trajectory finished on another
model would be a third model under the cheap one's name. The credential preflight below refuses
a role that could spill, in case the config ever changes underneath this script.

**Staged by request ceiling, never by task count**, as `scripts/a1_working.py` is and for its
reason: the declared task list is inside the fingerprint, so a run started over three tasks could
never be resumed into fifteen. Reaching ``--max-requests`` stops the run through its own guard
as ``operator``; tasks never started stay unrun and a resume picks them up.

    set -a && . ./.env && set +a
    uv run python scripts/a2_cheap.py --split smoke --max-requests 20
    uv run python scripts/a2_cheap.py --split smoke --run-id ID --max-requests 120
    uv run python scripts/a2_cheap.py --split working --max-requests 30
    uv run python scripts/a2_cheap.py --split working --run-id ID --max-requests 400
    uv run python scripts/a2_cheap.py --split working --run-id ID --project-only  # spends nothing

**A stage writes no results file.** Unlike 3.6's script, a stage prints its trajectories, the
rule's decisions and the two stop conditions below, and leaves `results/` alone, so a run that
spans days does not leave a partial projection in the working tree. ``--project-only`` writes
`results/a2-cheap-working.json` and its trajectory metrics from a working run. The smoke run
never writes one: its record is `docs/escalation-a2-cheap-smoke.json`, from
`scripts/escalation_table.py`.

**The two stop conditions, fixed with the author before the preflight spent anything.** After
every stage this reports, and exits 3 on either: (a) a request whose prompt and completion
tokens together exceeded the endpoint's per-minute budget — the event constraint 46 is about,
which the next request's wait would turn into a ``pools_exhausted`` stop; (b) a trajectory that
terminated on ``prompt_ceiling`` — conversations reaching the range where A1's measured density
puts one request over that budget (docs/escalation-a1-working.json, ``attempt_sizes``). Neither
moves a limit; both are stop-and-report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from query_pilot.agents import A1, TOOL_SCHEMAS, project, write_metrics, write_results
from query_pilot.agents.a1 import PROMPT_CEILING, REJECTED_GENERATION_POLICIES
from query_pilot.agents.attempts import loop_behaviour, read_attempt_sizes, summarise_sizes
from query_pilot.agents.escalation import CLAUSES, read_decisions, tabulate
from query_pilot.client import Client, ClientError
from query_pilot.client.config import ClientConfig
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
SPLITS = ("smoke", "working")
RESULTS_DIR = REPO / "results"
#: Beside `results/a1-trajectory-metrics.json`, and named for the same agent and split pair.
METRICS = RESULTS_DIR / "a2-cheap-trajectory-metrics.json"

#: Exit status when a stop condition trips, apart from 1 (the run is not finished).
STOP_CONDITION = 3


def declaration(split: str) -> Path:
    return REPO / "config" / "runs" / f"a2-cheap-{split}.toml"


def difficulties() -> dict[str, str]:
    """Spider's own parsed query per task, labelled by this project's rule.

    Identical to `scripts/a0_working.py`'s and `scripts/a1_working.py`'s, because every run
    compared on the working set must be labelled by one rule.
    """
    dev = json.loads((SPIDER / "dev.json").read_text())
    return {f"dev-{index:04d}": difficulty_of(item["sql"]) for index, item in enumerate(dev)}


def empty_reference_tasks(tasks) -> int:
    """How many of the split's reference queries return no rows, through the run's sandbox.

    Identical to 3.6's, so the floor beside this run's accuracy is measured the same way.
    """
    sandbox = Sandbox()
    copies = SubstrateCopies(SPIDER / "database", scope=CopyScope.RUN)
    empty = 0
    try:
        for task in tasks:
            with copies.for_task(task.db_id, task_id=task.task_id) as database:
                result = sandbox.execute(database, task.reference_sql)
            if not result.ok:
                raise SystemExit(f"{task.task_id}: the reference query does not execute")
            empty += not result.rows
    finally:
        copies.close()
    return empty


def role_of(config: RunConfig) -> str:
    role = config.params.get("role")
    if not role:
        raise SystemExit("the run declaration names no role in [run.params]")
    return str(role)


def rejected_generation_of(config: RunConfig) -> str:
    """What a refused generation does, read from the declaration and never defaulted here.

    A1's own default is 3.6's ``fail_task``; an A2-cheap declaration must say which rule it
    runs under, because the two produce figures that are not comparable.
    """
    policy = config.params.get("rejected_generation")
    if policy not in REJECTED_GENERATION_POLICIES:
        raise SystemExit(
            f"the run declaration must set rejected_generation to one of "
            f"{REJECTED_GENERATION_POLICIES} in [run.params], not {policy!r}"
        )
    return str(policy)


def preflight(client: Client, role: str) -> tuple[str, int]:
    """The credential, and that the role cannot spill. Returns the endpoint and its tpm."""
    try:
        candidates = client.registry.candidates(role)
    except ClientError as exc:
        print(f"{exc}\n\nLoad the keys first:  set -a && . ./.env && set +a", file=sys.stderr)
        raise SystemExit(2) from exc
    endpoints = {candidate.endpoint.name for candidate in candidates}
    if len(endpoints) != 1:
        raise SystemExit(
            f"role {role!r} reaches {sorted(endpoints)}; A2-cheap must be served by one model"
        )
    endpoint = candidates[0].endpoint
    if endpoint.limits.tpm is None:
        raise SystemExit(f"{endpoint.name} declares no tpm; constraint 64 cannot be checked")
    pools = ", ".join(candidate.credential.pool for candidate in candidates)
    print(f"role {role} -> {endpoint.name} ({endpoint.model}) on {pools}")
    return endpoint.name, int(endpoint.limits.tpm)


def describe(run_directory: Path, *, split: str, tpm: int) -> bool:
    """Print what the stage did, from the run directory alone. True if a stop condition tripped.

    The smoke split's report names no outcome per task: the preflight is read for how the loop
    behaved and which clauses fired, not for what solved.
    """
    decisions = read_decisions(run_directory)
    outcomes = split != "smoke"
    for row in decisions.decided:
        mark = ("solved   " if row.solved else "unsolved ") if outcomes else ""
        print(
            f"  {row.task_id}  {mark}{row.termination:<16} "
            f"first={row.first_execute_sql!s:<5} validation={row.validation_rule!s:<14} "
            f"clauses={','.join(row.escalation.clauses) or '-'}"
        )
    table = tabulate(decisions.decided, outcomes=outcomes)
    fired = {clause: table["clauses"][clause]["count"] for clause in CLAUSES}
    print(
        f"\n  decided {table['trajectories']}, undecided {len(decisions.undecided)}; "
        f"would escalate {table['would_escalate']['count']}; clauses {fired}; "
        f"no execute_sql {table['not_a_clause']['no_execute_sql']['count']}"
    )

    sizes = summarise_sizes(read_attempt_sizes(run_directory), tpm=tpm)
    print(f"  requests paired {sizes.get('attempts_paired')}, served by {sizes.get('served_by')}")
    if sizes.get("attempts_paired"):
        print(
            f"  largest request {sizes['largest_attempt']['tokens']} tokens "
            f"({sizes['largest_attempt']['share_of_tpm']:.1%} of tpm {tpm}); longest conversation "
            f"{sizes['longest_conversation']['chars']} chars "
            f"({sizes['longest_conversation']['share_of_ceiling']:.1%} of the ceiling); "
            f"chars/token {sizes['chars_per_prompt_token']}; fit {sizes['fit']}; "
            f"projected at ceiling {sizes['projected_attempt_at_ceiling']}"
        )
    print(f"  loop {json.dumps(loop_behaviour(run_directory))}")

    over = sizes.get("attempts_over_tpm", 0)
    ceilings = sum(1 for row in decisions.decided if row.termination == PROMPT_CEILING)
    failed = Counter(
        str(row.get("error_class"))
        for row in read_rows(run_directory / "ledger.jsonl")
        if row.get("kind") == "task" and row.get("status") != "complete"
    )
    if failed:
        print(f"  failed task rows by class: {dict(failed)}")
    tripped = bool(over or ceilings)
    if tripped:
        print(
            f"\nSTOP CONDITION: {over} request(s) over tpm, {ceilings} prompt_ceiling "
            f"termination(s). Stop and report; no limit moves (constraint 72).",
            file=sys.stderr,
        )
    return tripped


def project_working(ledger_path: Path, tasks) -> None:
    """Both committed files of the always-cheap run, from one run directory. Spends nothing."""
    document = project(
        ledger_path,
        split="working",
        difficulty=difficulties(),
        empty_reference_tasks=empty_reference_tasks(tasks),
    )
    written = write_results(document, RESULTS_DIR)
    write_metrics(ledger_path.parent, METRICS)
    print(json.dumps(document["execution_accuracy"], indent=2))
    print(json.dumps(document["tokens"], indent=2))
    print(f"\nresults {written}\nmetrics {METRICS}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=SPLITS, required=True)
    parser.add_argument("--run-id", default=None, help="resume an existing run instead")
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="per-session backstop on provider requests; required to spend anything",
    )
    parser.add_argument(
        "--project-only",
        action="store_true",
        help="report (and, for the working split, write results) from --run-id; spends nothing",
    )
    options = parser.parse_args()

    if not (SPIDER / "dev.json").exists():
        print("substrate not acquired; see docs/SUBSTRATE.md", file=sys.stderr)
        return 2

    tasks = load_tasks(SPIDER, REPO / "splits" / f"{options.split}.json")
    run_id = options.run_id or new_run_id()
    config = RunConfig.load(
        declaration(options.split), [task.task_id for task in tasks], run_id=run_id
    )
    role = role_of(config)
    policy = rejected_generation_of(config)

    if options.project_only:
        if not options.run_id:
            print("--project-only needs --run-id", file=sys.stderr)
            return 2
        ledger_path = RunLedger(run_id).path
        client_config = ClientConfig.load()
        tpm = client_config.endpoints[client_config.roles[role].endpoint].limits.tpm
        tripped = describe(ledger_path.parent, split=options.split, tpm=int(tpm or 0))
        if options.split == "working":
            project_working(ledger_path, tasks)
        return STOP_CONDITION if tripped else 0

    if options.max_requests is None:
        print("a run that spends quota needs --max-requests", file=sys.stderr)
        return 2

    client = Client.from_config()
    _, tpm = preflight(client, role)

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
        SPIDER / "database",
        ledger.directory,
        role=role,
        rejected_generation=policy,
    )

    print(
        f"run {run_id}: {len(tasks)} tasks ({options.split}), agent {config.agent}, "
        f"role {a1.role}, rejected generations {a1.rejected_generation}, "
        f"{len(TOOL_SCHEMAS)} tools, concurrency {config.concurrency}, "
        f"token ceiling {config.token_ceiling:,}, at most {options.max_requests} requests"
    )
    try:
        with ledger:
            report = await run.execute(a1)
    finally:
        a1.close()

    write_summary(summarise(ledger.path), ledger.path.parent)
    tripped = describe(ledger.directory, split=options.split, tpm=tpm)
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
    if tripped:
        return STOP_CONDITION
    if not report.complete or report.tasks_failed:
        print(
            f"\nThis run is NOT finished. Resume it:\n"
            f"    uv run python scripts/a2_cheap.py --split {options.split} --run-id {run_id} "
            f"--max-requests N",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
