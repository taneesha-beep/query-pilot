"""Run A1 end to end on a few tasks. **This spends real quota.**

**A1 has never met a real provider as a loop.** The four tools were driven live once — nine
requests, 2026-09-08, `docs/a1-tool-probe.json` — but the loop around them, its five
termination paths and 3.3's repair have only ever seen a stub. 2.3 had `scripts/a0_smoke.py`
before 2.4 measured, and this is A1's equivalent: the acceptance run that happens **before**
3.6 rather than inside it, so that 3.6 is a measurement and not a first meeting.

**What a stub cannot show, and this can.** Whether a real model, given four tools and no
schema, terminates on `answer` rather than wandering into the turn limit; whether the tool
results are readable enough to act on; whether a rejected reply comes back repaired; and
whether a fourteen-turn conversation stays under the per-minute token ceiling constraint 46
is about. Every one of those is a fact about the provider, and none of them is a result.

**No number taken here is reported anywhere.** The smoke set is drawn from inside the
working set precisely so that debugging costs a handful of tasks rather than 150, and 3.6 is
the measured run. The solved count this prints is an acceptance signal.

**The two ceilings, and why there are two.** The run declares a token ceiling and a
wall-clock ceiling like every other run, in `config/runs/a1-smoke.toml`. On top of those this
script counts **requests**, which no run config does, because A1's cost is not one request a
task: a trajectory may make up to `TURN_LIMIT + REPAIR_LIMIT` of them, so five tasks is
anywhere between five requests and seventy-five. The request ceiling is what makes the worst
case bounded in the unit a free tier is refused in. When it is reached the run is stopped
through its **own** guard, as `operator` — so the remaining tasks are unrun rather than
failed, the ledger says a person stopped it, and a resume picks them up.

**Concurrency is 1 and it is not a preference.** `PROMPT_CEILING_CHARS / 3.265 +
MAX_OUTPUT_TOKENS` is 8,000 tokens, which is the strong endpoint's whole per-minute budget
at one attempt. Constraint 46 leaves no room for a second in flight.

**The keys are not loaded for you**, the same as `scripts/a0_smoke.py` and
`scripts/client_smoke.py`::

    set -a && . ./.env && set +a
    uv run python scripts/a1_smoke.py [--limit 5] [--max-requests 80] [--run-id ID]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from query_pilot.agents import A1, MAX_OUTPUT_TOKENS, PROMPT_CEILING_CHARS, ROLE, TOOL_SCHEMAS
from query_pilot.client import Client, ClientError
from query_pilot.client.types import Completion, Message, ToolSchema
from query_pilot.run import Run, RunConfig, RunLedger, new_run_id, read_rows
from query_pilot.run.guard import BudgetGuard, IncompleteReason
from query_pilot.tasks import load_tasks

REPO = Path(__file__).resolve().parent.parent
SPIDER = REPO / "data" / "spider"
SMOKE = REPO / "splits" / "smoke.json"
RUN_CONFIG = REPO / "config" / "runs" / "a1-smoke.toml"


class RequestCeiling:
    """A client that counts requests and stops the **run** when it has made too many.

    Wrapping rather than extending, and stopping the run rather than raising, are both
    deliberate. Raising would fail one task and let the next one start, which is the opposite
    of a ceiling. Calling :meth:`BudgetGuard.stop` puts the run down the path it already has
    for a person deciding to stop it: A1 sees it at the next turn boundary, the tasks that
    never started are recorded unrun rather than failed, and a resume picks them up.
    """

    def __init__(self, client: Client, guard: BudgetGuard, ceiling: int) -> None:
        self.client = client
        self.guard = guard
        self.ceiling = ceiling
        self.requests = 0

    async def complete(
        self,
        role: str,
        messages: list[Message],
        tools: tuple[ToolSchema, ...] | None = None,
        **kwargs: object,
    ) -> Completion:
        self.requests += 1
        if self.requests >= self.ceiling:
            print(f"request ceiling of {self.ceiling} reached; stopping the run", file=sys.stderr)
            self.guard.stop(IncompleteReason.OPERATOR)
        return await self.client.complete(role, messages, tools, **kwargs)  # type: ignore[arg-type]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5, help="how many smoke tasks to run")
    parser.add_argument(
        "--max-requests",
        type=int,
        default=80,
        help="hard ceiling on provider requests across the whole run",
    )
    parser.add_argument("--run-id", default=None, help="resume an existing run instead")
    options = parser.parse_args()

    if not (SPIDER / "dev.json").exists():
        print("substrate not acquired; see docs/SUBSTRATE.md", file=sys.stderr)
        return 2

    tasks = load_tasks(SPIDER, SMOKE)[: options.limit]
    run_id = options.run_id or new_run_id()
    config = RunConfig.load(RUN_CONFIG, [task.task_id for task in tasks], run_id=run_id)

    client = Client.from_config()

    # Preflight, for the reason 2.3's script documents: a missing key is one fact about the
    # environment, and finding it out once here beats every task discovering it separately.
    try:
        client.registry.candidates(ROLE)
    except ClientError as exc:
        print(f"{exc}\n\nLoad the keys first:  set -a && . ./.env && set +a", file=sys.stderr)
        return 2

    ledger = RunLedger(run_id)
    run = Run(config, ledger)
    counted = RequestCeiling(client, run.guard, options.max_requests)
    a1 = A1(counted, tasks, SPIDER / "database", ledger.directory)  # type: ignore[arg-type]

    print(
        f"run {run_id}: {len(tasks)} tasks, agent {config.agent}, role {a1.role}, "
        f"{len(TOOL_SCHEMAS)} tools, at most {options.max_requests} requests"
    )
    try:
        with ledger:
            report = await run.execute(a1)
    finally:
        a1.close()

    solved = 0
    terminations: Counter[str] = Counter()
    repairs = repaired = 0
    for row in read_rows(ledger.path):
        if row["kind"] != "task" or row["status"] != "complete":
            continue
        detail = row["detail"]
        solved += bool(detail.get("solved"))
        terminations[detail["termination"]] += 1
        repairs += detail.get("repair_attempts") or 0
        repaired += bool(detail.get("repair_succeeded"))
        mark = "solved  " if detail.get("solved") else f"{detail['reason']:<8}"
        print(
            f"  {row['task_id']}  {mark}  {detail['db_id']:<22} "
            f"{detail['termination']:<16} {detail['turns']:>2}t "
            f"{detail['tool_calls']:>2}c  {detail['sql']!r}"
        )
        print(f"      tools: {detail['tool_calls_by_name']}")
        if detail.get("repair_attempts"):
            print(f"      repair: succeeded={detail['repair_succeeded']}")
        if detail.get("repair_blocked"):
            print(f"      repair blocked by {detail['repair_blocked']}")
        if not detail.get("solved"):
            print(f"      reference: {detail['reference_sql']!r}")
            if detail.get("detail"):
                print(f"      {detail['detail']}")

    print(
        json.dumps(
            {
                "run_id": run_id,
                "status": report.status,
                "incomplete_reason": report.incomplete_reason,
                "tasks_complete": report.tasks_complete,
                "tasks_failed": report.tasks_failed,
                "solved": solved,
                "requests": counted.requests,
                "terminations": dict(terminations),
                "repair_attempts": repairs,
                "repair_successes": repaired,
                "prompt_tokens": report.prompt_tokens,
                "completion_tokens": report.completion_tokens,
                "elapsed_s": round(report.elapsed_s, 2),
                "ledger": str(ledger.path),
                "transcripts": str(ledger.directory / "transcripts"),
            },
            indent=2,
        )
    )
    print(
        "\nAn acceptance run on the smoke set. The solved count above is NOT a result and is "
        "reported nowhere: 3.6 is A1's measured run.\n"
        f"One attempt's worst case is {PROMPT_CEILING_CHARS / 3.265 + MAX_OUTPUT_TOKENS:.0f} "
        "tokens against the strong endpoint's 8,000 a minute, which is why concurrency is 1."
    )
    # Non-zero on any failed task, not only on a ceiling. Constraint 44: a run status of
    # `complete` means "not cut short", never "it worked".
    return 0 if report.complete and report.tasks_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
