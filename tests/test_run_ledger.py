"""The run ledger: what it records, and what a killed run can resume from.

**No test here makes a live API call and none needs a key.** The run loop works over an
injected callable that executes one task, and every task below is a stub that counts how
often it was asked. Nothing in this file knows what a task is, because nothing in
`query_pilot.run` does either.

One test kills a real subprocess with SIGKILL. It is the only test in this project that
proves the durability claim rather than assuming it: an in-process simulation cannot show
that a row survived the process, only that a function was called.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import GOOGLE_DAILY_429
from query_pilot.client.errors import ProviderHTTPError, QuotaFact, TransportError
from query_pilot.client.scheduler import QuotaWall
from query_pilot.client.types import Completion, RateLimit
from query_pilot.run import (
    COMPLETE,
    FAILED,
    AttemptRow,
    Run,
    RunConfig,
    RunConfigChanged,
    RunError,
    RunLedger,
    TaskResult,
    new_run_id,
    read_ledger,
    read_rows,
)

SRC = Path(__file__).resolve().parents[1] / "src"


class FixedClock:
    """Only ``now_utc`` matters for a run ID; the rest is never asked for here."""

    def __init__(self, wall: datetime) -> None:
        self.wall = wall

    def monotonic(self) -> float:
        return 0.0

    def now_utc(self) -> datetime:
        return self.wall

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - never called
        return None


# --- material ---------------------------------------------------------------------------
# Nothing about databases or queries: this package must not learn what it is used for.


def completion(
    *,
    provider: str = "groq",
    model: str = "openai/gpt-oss-20b",
    pool: str = "groq#1",
    prompt_tokens: int = 80,
    completion_tokens: int = 20,
    model_returned: str | None = None,
) -> Completion:
    return Completion(
        text="ready",
        tool_calls=(),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        provider=provider,
        model=model,
        pool=pool,
        latency_s=0.61,
        model_returned=model_returned,
        finish_reason="stop",
        rate_limit=RateLimit(requests_remaining=926),
        raw={"id": "stub"},
    )


def ledger_for(tmp_path: Path, run_id: str = "r-1") -> RunLedger:
    return RunLedger(run_id, root=tmp_path / "runs")


def config_for(task_ids, *, run_id: str = "r-1", agent: str = "A0", concurrency: int = 1, **kw):
    """Ceilings high enough that the budget guard never fires; 1.4's tests are where it does."""
    kw.setdefault("token_ceiling", 10_000_000)
    kw.setdefault("wall_clock_ceiling_s", 86_400.0)
    return RunConfig.start(agent, list(task_ids), run_id=run_id, concurrency=concurrency, **kw)


def kinds(path: Path) -> list[str]:
    return [row["kind"] for row in read_rows(path)]


def rows_of(path: Path, kind: str) -> list[dict]:
    return [row for row in read_rows(path) if row["kind"] == kind]


# --- what a row carries -------------------------------------------------------------------


def test_an_attempt_row_carries_the_pool_and_the_model_that_actually_answered():
    row = AttemptRow.answered(
        completion(pool="google-ai-studio#2", model_returned="gemini-3.5-flash-lite-001"),
        run_id="r-1",
        task_id="t-1",
        agent="A0",
        recorded_at="2026-09-08T00:00:00+00:00",
    )
    # Neither field is in the roadmap's list and both are load-bearing: buckets are per
    # pool per model, and Google answers a pinned request with its own build string.
    assert row.pool == "google-ai-studio#2"
    assert row.model == "openai/gpt-oss-20b"
    assert row.model_returned == "gemini-3.5-flash-lite-001"
    assert row.outcome == "ok"
    assert row.total_tokens == 100


def test_a_refused_attempt_records_the_classifiers_own_label_not_a_second_taxonomy():
    error = ProviderHTTPError(
        status=429,
        body=GOOGLE_DAILY_429,
        provider="google-ai-studio",
        model="gemini-3.8-flash",
        pool="google-ai-studio#1",
        quota=QuotaFact(quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier"),
    )
    row = AttemptRow.refused(
        error, run_id="r-1", task_id="t-1", agent="A0", recorded_at="2026-09-08T00:00:00+00:00"
    )
    assert row.error_class == "quota_day"
    assert row.outcome == "error"
    assert (row.provider, row.model, row.pool) == (
        "google-ai-studio",
        "gemini-3.8-flash",
        "google-ai-studio#1",
    )


def test_a_transport_failure_reaches_the_ledger_as_itself():
    error = TransportError(
        "read timeout", provider="groq", model="openai/gpt-oss-20b", pool="groq#1"
    )
    row = AttemptRow.refused(
        error, run_id="r-1", task_id="t-1", agent="A0", recorded_at="2026-09-08T00:00:00+00:00"
    )
    assert row.error_class == "transport"


# --- the file ------------------------------------------------------------------------------


async def test_a_row_is_on_disk_before_the_next_attempt_starts(tmp_path):
    """The requirement is a durability one, so the test reads the file, not a counter."""
    seen: list[int] = []

    async def executor(context):
        context.record(completion())
        seen.append(len(rows_of(context.run.ledger.path, "attempt")))
        context.record(completion(), turn=2)
        seen.append(len(rows_of(context.run.ledger.path, "attempt")))
        return TaskResult()

    ledger = ledger_for(tmp_path)
    with ledger:
        await Run(config_for(["t-1"]), ledger).execute(executor)
    assert seen == [1, 2]


async def test_the_five_kinds_share_one_file_in_the_order_they_happened(tmp_path):
    async def executor(context):
        context.record(completion())
        context.run.ledger.on_quota_wall(wall())
        return TaskResult(detail={"note": "stub"})

    ledger = ledger_for(tmp_path)
    with ledger:
        await Run(config_for(["t-1"]), ledger).execute(executor)
    assert kinds(ledger.path) == ["run_start", "attempt", "wall", "task", "run_end"]


def wall() -> QuotaWall:
    return QuotaWall(
        observed_at="2026-09-08T00:00:00+00:00",
        role="cheap",
        provider="google-ai-studio",
        pool="google-ai-studio#1",
        model="gemini-3.8-flash",
        status=429,
        reason="quota_day",
        scope="day",
        blocked_for_s=46800.0,
        quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        quota_value=20,
        body=GOOGLE_DAILY_429,
    )


async def test_a_quota_wall_lands_in_the_run_that_hit_it_with_its_body_whole(tmp_path):
    """1.2's sink, taken over. Google's daily allowance is measured from exactly this."""
    ledger = ledger_for(tmp_path)
    with ledger:
        ledger.on_quota_wall(wall())
    (row,) = rows_of(ledger.path, "wall")
    assert row["run_id"] == "r-1"
    assert row["quota_id"] == "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    assert row["quota_value"] == 20
    assert row["body"] == GOOGLE_DAILY_429


async def test_a_wall_recorded_after_the_ledger_closed_is_still_written(tmp_path):
    """A sink handed to a client outlives the block that opened the ledger."""
    ledger = ledger_for(tmp_path)
    with ledger:
        ledger.append({"kind": "run_start", "run_id": "r-1"})
    ledger.on_quota_wall(wall())
    assert len(rows_of(ledger.path, "wall")) == 1


async def test_one_file_stays_append_only_under_the_concurrency_the_client_allows(tmp_path):
    """Eight in flight finish at once; the append has no await in it, so none interleave."""
    ledger = ledger_for(tmp_path)
    with ledger:

        async def append(index: int) -> None:
            await asyncio.sleep(0)
            ledger.append({"kind": "attempt", "run_id": "r-1", "task_id": f"t-{index}"})

        await asyncio.gather(*(append(i) for i in range(200)))

    lines = ledger.path.read_text().splitlines()
    assert len(lines) == 200
    assert all(json.loads(line)["kind"] == "attempt" for line in lines)
    assert {json.loads(line)["task_id"] for line in lines} == {f"t-{i}" for i in range(200)}


def test_a_partial_last_line_is_counted_and_skipped_rather_than_fatal(tmp_path):
    """A refusal to read a truncated ledger is how a killed run becomes unrecoverable."""
    ledger = ledger_for(tmp_path)
    with ledger:
        ledger.append({"kind": "task", "run_id": "r-1", "task_id": "t-1", "status": COMPLETE})
    with ledger.path.open("a") as handle:
        handle.write('{"kind": "task", "run_id": "r-1", "task_i')

    state = read_ledger(ledger.path)
    assert state.complete_task_ids == {"t-1"}
    assert state.unreadable_lines == 1


# --- the declaration -------------------------------------------------------------------------


def test_a_run_id_is_supplied_or_generated_and_a_generated_one_sorts_chronologically():
    assert config_for(["t-1"], run_id="given").run_id == "given"

    early = FixedClock(datetime(2026, 9, 8, 9, 0, tzinfo=UTC))
    late = FixedClock(datetime(2026, 9, 8, 17, 30, tzinfo=UTC))
    first = new_run_id(early, random.Random(1))
    second = new_run_id(late, random.Random(1))

    assert first == "20260908-090000-44cb63"  # deterministic given a clock and a seed
    assert first < second  # a directory listing is chronological
    # Two runs started in the same second stay distinct.
    assert new_run_id(early, random.Random(1)) != new_run_id(early, random.Random(2))


def test_a_run_writes_under_runs_slash_its_own_id(tmp_path):
    ledger = RunLedger("20260908-090000-44cb63", root=tmp_path / "runs")
    assert ledger.path == tmp_path / "runs" / "20260908-090000-44cb63" / "ledger.jsonl"


async def test_a_bug_in_the_executor_is_not_filed_under_a_providers_failure_class(tmp_path):
    """Phase 2.5 counts failure classes. A KeyError in our own code is not one of them."""

    async def broken(context):
        context.record(completion())
        raise KeyError("schema")

    ledger = ledger_for(tmp_path)
    with ledger:
        report = await Run(config_for(["t-1"]), ledger).execute(broken)

    assert report.tasks_failed == 1 and report.status == "complete"
    (row,) = rows_of(ledger.path, "task")
    assert row["error_class"] == "executor_error"
    # The type is what 1.3 decided goes here. The message goes beside it: a row carrying
    # only the class name is a failure nobody can diagnose without re-running it, which is
    # exactly what 2.5 cannot do.
    assert row["detail"] == {"exception": "KeyError", "message": "'schema'"}


def test_a_run_that_lists_a_task_twice_is_refused():
    # Two rows for one task ID collapse under the skip rule and the run would measure it
    # once without saying so.
    with pytest.raises(RunError, match="duplicate task IDs"):
        config_for(["t-1", "t-2", "t-1"])


def test_a_run_with_no_tasks_or_no_agent_is_refused():
    with pytest.raises(RunError, match="declares no tasks"):
        config_for([])
    with pytest.raises(RunError, match="must name its agent"):
        config_for(["t-1"], agent="")


def test_a_run_cannot_be_declared_without_both_ceilings():
    """Not optional with a default: that is a run declaring neither while looking equipped."""
    with pytest.raises(TypeError):
        RunConfig.start("A0", ["t-1"], run_id="r-1")
    with pytest.raises(RunError, match="token_ceiling must be positive"):
        config_for(["t-1"], token_ceiling=0)
    with pytest.raises(RunError, match="wall_clock_ceiling_s must be positive"):
        config_for(["t-1"], wall_clock_ceiling_s=0)


def test_the_fingerprint_covers_the_declaration_and_not_the_identity():
    one = config_for(["t-1", "t-2"], run_id="a")
    two = config_for(["t-1", "t-2"], run_id="b")
    assert one.fingerprint() == two.fingerprint()
    assert config_for(["t-2", "t-1"], run_id="a").fingerprint() != one.fingerprint()
    assert (
        config_for(["t-1", "t-2"], run_id="a", params={"turns": 6}).fingerprint()
        != one.fingerprint()
    )
    # The ceilings are inside it on purpose: raising a budget mid-run must not be quiet.
    assert (
        config_for(["t-1", "t-2"], run_id="a", token_ceiling=99).fingerprint() != one.fingerprint()
    )


async def test_resuming_a_run_id_with_different_parameters_is_refused(tmp_path):
    async def executor(context):
        return TaskResult()

    ledger = ledger_for(tmp_path)
    with ledger:
        await Run(config_for(["t-1", "t-2"]), ledger).execute(executor)
        # Same ID, a different task list: this is how one file quietly becomes two
        # experiments, and it is the whole reason the fingerprint is recorded.
        with pytest.raises(RunConfigChanged, match="two experiments in one ledger"):
            await Run(config_for(["t-1", "t-9"]), ledger).execute(executor)


async def test_a_deliberate_change_is_allowed_and_both_fingerprints_are_recorded(tmp_path):
    async def executor(context):
        return TaskResult()

    ledger = ledger_for(tmp_path)
    first = config_for(["t-1"])
    with ledger:
        await Run(first, ledger).execute(executor)
        changed = config_for(["t-1", "t-2"])
        await Run(changed, ledger).execute(executor, allow_config_change=True)

    start = rows_of(ledger.path, "run_start")[-1]
    assert start["previous_fingerprint"] == first.fingerprint()
    assert start["fingerprint"] == changed.fingerprint()


def test_a_ledger_and_a_config_must_name_the_same_run(tmp_path):
    with pytest.raises(RunError, match="one ledger holds one run"):
        Run(config_for(["t-1"], run_id="r-1"), ledger_for(tmp_path, "r-2"))


# --- resume -----------------------------------------------------------------------------------


class OperatorKill(BaseException):
    """Stands in for an operator's interrupt.

    A ``BaseException`` and deliberately not an ``Exception``, because that is the line the
    loop draws: a task that fails is data, and a kill is not. Literal ``KeyboardInterrupt``
    is avoided only because pytest reads one as a request to abandon the session.
    """


class CountingExecutor:
    """Counts how often each task was asked for, and can be told to stop partway."""

    def __init__(self, *, stop_after: int | None = None, fail: set[str] | None = None):
        self.calls: list[str] = []
        self.stop_after = stop_after
        self.fail = fail or set()

    async def __call__(self, context):
        if self.stop_after is not None and len(self.calls) >= self.stop_after:
            # Not an Exception: an operator's kill is not a task failure, and the loop
            # must not record it as one.
            raise OperatorKill("killed")
        self.calls.append(context.task_id)
        context.record(completion())
        if context.task_id in self.fail:
            raise TransportError(
                "read timeout", provider="groq", model="openai/gpt-oss-20b", pool="groq#1"
            )
        return TaskResult()


async def test_a_run_killed_halfway_resumes_without_repeating_or_skipping_a_task(tmp_path):
    """The acceptance test: kill it, restart it, count what each task got."""
    tasks = [f"t-{i}" for i in range(6)]
    ledger = ledger_for(tmp_path)
    config = config_for(tasks)

    first = CountingExecutor(stop_after=3)
    with ledger, pytest.raises(OperatorKill):
        await Run(config, ledger).execute(first)
    assert first.calls == tasks[:3]

    second = CountingExecutor()
    with ledger:
        report = await Run(config, ledger).execute(second)

    assert second.calls == tasks[3:]  # none repeated
    assert sorted(first.calls + second.calls) == sorted(tasks)  # none skipped
    assert report.tasks_complete == 6 and report.tasks_remaining == 0
    assert report.resumed and report.complete

    done = [row["task_id"] for row in rows_of(ledger.path, "task") if row["status"] == COMPLETE]
    assert sorted(done) == sorted(tasks) and len(done) == len(set(done))


async def test_an_interrupt_this_process_can_catch_is_recorded_rather_than_inferred(tmp_path):
    """An interrupt is catchable, so the run still writes why it stopped.

    Only a kill the process cannot catch leaves a segment without a ``run_end``, and the
    SIGKILL test below is where that claim is actually made.
    """
    ledger = ledger_for(tmp_path)
    config = config_for(["t-1", "t-2"])
    with ledger, pytest.raises(OperatorKill):
        await Run(config, ledger).execute(CountingExecutor(stop_after=1))

    state = read_ledger(ledger.path)
    assert not state.unclean
    assert state.segments[-1].status == "incomplete"
    assert state.segments[-1].incomplete_reason == "operator"

    with ledger:
        await Run(config, ledger).execute(CountingExecutor())
    assert rows_of(ledger.path, "run_start")[1]["resumed"] is True
    assert read_ledger(ledger.path).segments[-1].status == "complete"


async def test_a_task_cut_off_mid_attempt_is_run_again_and_recorded_once(tmp_path):
    """No start row exists, so a half attempt cannot: the task simply has no answer yet."""
    ledger = ledger_for(tmp_path)
    config = config_for(["t-1", "t-2"])

    async def cut_off(context):
        context.record(completion())  # the attempt is on disk
        if context.task_id == "t-2":
            raise OperatorKill("killed mid-task")
        return TaskResult()

    with ledger, pytest.raises(OperatorKill):
        await Run(config, ledger).execute(cut_off)
    assert [r["task_id"] for r in rows_of(ledger.path, "attempt")] == ["t-1", "t-2"]
    assert [r["task_id"] for r in rows_of(ledger.path, "task")] == ["t-1"]

    second = CountingExecutor()
    with ledger:
        await Run(config, ledger).execute(second)

    assert second.calls == ["t-2"]
    complete = [r["task_id"] for r in rows_of(ledger.path, "task") if r["status"] == COMPLETE]
    assert complete == ["t-1", "t-2"]
    # Two attempts on t-2: the first was paid for and never answered. Recorded, not hidden.
    assert len([r for r in rows_of(ledger.path, "attempt") if r["task_id"] == "t-2"]) == 2


async def test_a_failed_task_is_retried_on_resume_and_a_complete_one_is_not(tmp_path):
    """Complete means the run got an answer. Failed means it never managed to ask."""
    ledger = ledger_for(tmp_path)
    config = config_for(["t-1", "t-2", "t-3"])

    with ledger:
        report = await Run(config, ledger).execute(CountingExecutor(fail={"t-2"}))
    assert report.tasks_complete == 2 and report.tasks_failed == 1
    assert report.status == "complete"  # the run finished asking; one task had no answer

    (failed,) = [r for r in rows_of(ledger.path, "task") if r["status"] == FAILED]
    assert failed["task_id"] == "t-2" and failed["error_class"] == "transport"

    second = CountingExecutor()
    with ledger:
        await Run(config, ledger).execute(second)
    assert second.calls == ["t-2"]

    state = read_ledger(ledger.path)
    assert state.complete_task_ids == {"t-1", "t-2", "t-3"} and not state.failed_task_ids


async def test_a_finished_run_resumed_again_does_nothing(tmp_path):
    ledger = ledger_for(tmp_path)
    config = config_for(["t-1", "t-2"])
    with ledger:
        await Run(config, ledger).execute(CountingExecutor())
        again = CountingExecutor()
        report = await Run(config, ledger).execute(again)
    assert again.calls == [] and report.tasks_complete == 2


async def test_totals_are_cumulative_across_resumes_because_a_budget_is(tmp_path):
    ledger = ledger_for(tmp_path)
    config = config_for(["t-1", "t-2", "t-3", "t-4"])
    with ledger, pytest.raises(OperatorKill):
        await Run(config, ledger).execute(CountingExecutor(stop_after=2))
    with ledger:
        report = await Run(config, ledger).execute(CountingExecutor())

    assert report.attempts == 4  # two from each session
    assert report.total_tokens == 4 * 100
    assert read_ledger(ledger.path).total_tokens == 400


# --- concurrency -----------------------------------------------------------------------------


async def test_the_loop_runs_no_more_tasks_at_once_than_the_run_declares(tmp_path):
    in_flight = 0
    peak = 0

    async def executor(context):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        context.record(completion())
        in_flight -= 1
        return TaskResult()

    ledger = ledger_for(tmp_path)
    config = config_for([f"t-{i}" for i in range(40)], concurrency=8)
    with ledger:
        report = await Run(config, ledger).execute(executor)

    assert peak == 8
    assert report.tasks_complete == 40
    assert len({r["task_id"] for r in rows_of(ledger.path, "task")}) == 40


# --- the kill that is real -------------------------------------------------------------------

CHILD = textwrap.dedent(
    """
    import asyncio, sys
    from query_pilot.run import Run, RunConfig, RunLedger, TaskResult
    from query_pilot.client.types import Completion, RateLimit

    root, run_id = sys.argv[1], sys.argv[2]
    tasks = [f"t-{i}" for i in range(6)]

    def completion():
        return Completion(text="ready", tool_calls=(), prompt_tokens=80,
                          completion_tokens=20, provider="groq", model="openai/gpt-oss-20b",
                          pool="groq#1", latency_s=0.61, finish_reason="stop")

    async def executor(context):
        context.record(completion())
        if context.task_id == "t-3":
            await asyncio.sleep(3600)   # hang here; the parent kills us
        return TaskResult()

    async def main():
        ledger = RunLedger(run_id, root=root)
        config = RunConfig.start("A0", tasks, run_id=run_id, concurrency=1,
                                 token_ceiling=10_000_000, wall_clock_ceiling_s=86_400.0)
        with ledger:
            await Run(config, ledger).execute(executor)

    asyncio.run(main())
    """
)


def test_a_process_killed_with_sigkill_leaves_a_ledger_that_resumes(tmp_path):
    """The only test here that proves fsync did something, rather than assuming it.

    An in-process kill shows the loop's logic. It cannot show that a row outlived the
    process, and that is the claim the acceptance test actually makes.
    """
    root = tmp_path / "runs"
    ledger = RunLedger("r-kill", root=root)
    script = tmp_path / "child.py"
    script.write_text(CHILD)

    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONUNBUFFERED": "1"}
    child = subprocess.Popen([sys.executable, str(script), str(root), "r-kill"], env=env)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if ledger.path.exists() and len(rows_of(ledger.path, "task")) >= 3:
                break
            if child.poll() is not None:
                raise AssertionError(f"child exited early with {child.returncode}")
            time.sleep(0.02)
        else:
            raise AssertionError("child never recorded three tasks")
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        if child.poll() is None:  # pragma: no cover - only on an unexpected failure
            child.kill()
            child.wait(timeout=10)

    state = read_ledger(ledger.path)
    assert state.complete_task_ids == {"t-0", "t-1", "t-2"}
    assert state.unclean  # SIGKILL cannot write a run_end, and its absence is the record
    # The attempt on t-3 was made and recorded; the task has no answer, so it is retried.
    assert {r["task_id"] for r in rows_of(ledger.path, "attempt")} == {"t-0", "t-1", "t-2", "t-3"}

    resumed = CountingExecutor()
    config = config_for([f"t-{i}" for i in range(6)], run_id="r-kill")
    with ledger:
        report = asyncio.run(Run(config, ledger).execute(resumed))

    assert resumed.calls == ["t-3", "t-4", "t-5"]
    assert report.tasks_complete == 6 and report.after_unclean_shutdown
