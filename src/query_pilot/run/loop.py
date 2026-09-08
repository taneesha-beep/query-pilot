"""Drive a list of tasks, record every attempt, and resume where a kill left off.

**Nothing here knows what a task is.** The loop is handed a callable that executes one,
and every test in this package passes a stub. A0 does not exist until 2.3, and if anything
in this module ever learns what a task contains, it is in the wrong file.

The contract with that callable is Python's own: **returning means the run got an answer
for the task, raising means it did not.** Whether the answer was *right* is a question for
Phase 2, which puts it in the result's ``detail`` where this module never looks.

The resume rule, decided here rather than left to 2.4:

- A task with a ``complete`` row is **skipped**. Re-running it would replace a recorded
  measurement with a second one, which is how a resumed run silently becomes two
  experiments.
- A task with a ``failed`` row is **retried**. It has no answer, only a record that the
  run could not ask. Its old rows stay in the file, so nothing is lost.
- A task with no terminal row at all — including one whose attempt was in flight when the
  process was killed — is **run**. Nothing was recorded for it, so nothing is double
  counted; only provider quota was spent twice, which the client's buckets already see.

What this guarantees is therefore precise: **no task is recorded complete twice, and no
task is left without a terminal outcome when the run reports it finished.** A task cut off
mid-attempt is executed again, and that is the correct behaviour rather than a hole in it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from query_pilot.client.classify import classify
from query_pilot.client.clock import Clock, SystemClock
from query_pilot.client.errors import ClientError
from query_pilot.client.types import Completion
from query_pilot.run.config import RunConfig, RunConfigChanged, RunError
from query_pilot.run.ledger import (
    COMPLETE,
    EXECUTOR_ERROR,
    FAILED,
    AttemptRow,
    LedgerState,
    RunEndRow,
    RunLedger,
    RunStartRow,
    TaskRow,
)

STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class TaskResult:
    """What an executor returns. ``detail`` is free-form and never inspected here."""

    detail: Mapping[str, Any] = field(default_factory=dict)


class TaskContext:
    """Handed to the executor. Its job is to get an attempt on disk immediately.

    Attempts are recorded through this rather than returned at the end of the task,
    because the requirement is that a row is written **before the next attempt starts** —
    a task that reported five attempts on completion would leave four of them unwritten
    while the fifth was running.
    """

    def __init__(self, run: Run, task_id: str) -> None:
        self.run = run
        self.task_id = task_id
        self.attempts = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.last_error_class: str | None = None

    @property
    def run_id(self) -> str:
        return self.run.config.run_id

    @property
    def agent(self) -> str:
        return self.run.config.agent

    def record(self, completion: Completion, *, turn: int = 1) -> AttemptRow:
        """An answered attempt."""
        self.attempts += 1
        row = AttemptRow.answered(
            completion,
            run_id=self.run_id,
            task_id=self.task_id,
            agent=self.agent,
            recorded_at=self.run.ledger.now(),
            turn=turn,
            attempt=self.attempts,
        )
        self.prompt_tokens += row.prompt_tokens or 0
        self.completion_tokens += row.completion_tokens or 0
        self.run.write_attempt(row)
        return row

    def record_failure(
        self,
        error: ClientError,
        *,
        turn: int = 1,
        provider: str | None = None,
        model: str | None = None,
        pool: str | None = None,
        latency_s: float | None = None,
    ) -> AttemptRow:
        """A refused attempt, classified by the client's rule rather than a second one."""
        self.attempts += 1
        row = AttemptRow.refused(
            error,
            run_id=self.run_id,
            task_id=self.task_id,
            agent=self.agent,
            recorded_at=self.run.ledger.now(),
            turn=turn,
            attempt=self.attempts,
            provider=provider,
            model=model,
            pool=pool,
            latency_s=latency_s,
        )
        self.last_error_class = row.error_class
        self.run.write_attempt(row)
        return row


TaskExecutor = Callable[[TaskContext], Awaitable[TaskResult | None]]


@dataclass(frozen=True, slots=True)
class RunReport:
    """What one call to :meth:`Run.execute` did.

    Token and task counts are **cumulative for the run** — they include what earlier
    segments recorded — because that is what a budget is spent against. ``elapsed_s`` is
    **this session only**, because a run designed to span a quota reset spends most of its
    calendar time not running.
    """

    run_id: str
    agent: str
    status: str
    tasks_declared: int
    tasks_complete: int
    tasks_failed: int
    tasks_remaining: int
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    elapsed_s: float
    resumed: bool = False
    after_unclean_shutdown: bool = False
    incomplete_reason: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def complete(self) -> bool:
        return self.status == STATUS_COMPLETE


class Run:
    """One pass over the tasks a :class:`RunConfig` declares, resumable by run ID."""

    def __init__(
        self,
        config: RunConfig,
        ledger: RunLedger,
        *,
        clock: Clock | None = None,
    ) -> None:
        if ledger.run_id != config.run_id:
            raise RunError(
                f"ledger is for run {ledger.run_id!r} but the config declares "
                f"{config.run_id!r}; one ledger holds one run"
            )
        self.config = config
        self.ledger = ledger
        self.clock = clock or SystemClock()
        self.attempts = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._complete: set[str] = set()
        self._failed: set[str] = set()

    # -- recording -----------------------------------------------------------------------
    def write_attempt(self, row: AttemptRow) -> None:
        self.attempts += 1
        self.prompt_tokens += row.prompt_tokens or 0
        self.completion_tokens += row.completion_tokens or 0
        self.ledger.record_attempt(row)

    def _write_task(
        self,
        context: TaskContext,
        status: str,
        elapsed_s: float,
        detail: Mapping[str, Any],
    ) -> None:
        self.ledger.record_task(
            TaskRow(
                run_id=self.config.run_id,
                task_id=context.task_id,
                agent=self.config.agent,
                status=status,
                recorded_at=self.ledger.now(),
                attempts=context.attempts,
                prompt_tokens=context.prompt_tokens,
                completion_tokens=context.completion_tokens,
                elapsed_s=elapsed_s,
                error_class=None if status == COMPLETE else context.last_error_class,
                detail=dict(detail),
            )
        )
        if status == COMPLETE:
            self._complete.add(context.task_id)
            self._failed.discard(context.task_id)
        else:
            self._failed.add(context.task_id)

    # -- the pass ------------------------------------------------------------------------
    def plan(self, state: LedgerState, *, allow_config_change: bool = False) -> tuple[str, ...]:
        """Which declared tasks still need running, and refuse a changed declaration."""
        offered = self.config.fingerprint()
        recorded = state.fingerprint
        if recorded and recorded != offered and not allow_config_change:
            raise RunConfigChanged(self.config.run_id, recorded=recorded, offered=offered)
        self._complete = set(state.complete_task_ids)
        self._failed = set(state.failed_task_ids) - self._complete
        self.attempts = state.attempts
        self.prompt_tokens = state.prompt_tokens
        self.completion_tokens = state.completion_tokens
        return tuple(t for t in self.config.task_ids if t not in self._complete)

    async def execute(
        self,
        executor: TaskExecutor,
        *,
        allow_config_change: bool = False,
    ) -> RunReport:
        """Run every task that is not already recorded complete."""
        state = self.ledger.read()
        remaining = self.plan(state, allow_config_change=allow_config_change)
        started = self.clock.monotonic()

        self.ledger.record_start(
            RunStartRow(
                run_id=self.config.run_id,
                agent=self.config.agent,
                fingerprint=self.config.fingerprint(),
                started_at=self.ledger.now(),
                declared=self.config.declared(),
                tasks_declared=len(self.config.task_ids),
                tasks_remaining=len(remaining),
                resumed=state.exists,
                after_unclean_shutdown=state.unclean,
                previous_fingerprint=state.fingerprint,
            )
        )

        await self._drive(executor, remaining)

        elapsed = self.clock.monotonic() - started
        report = self._report(STATUS_COMPLETE, None, elapsed, state)
        self.ledger.record_end(
            RunEndRow(
                run_id=self.config.run_id,
                agent=self.config.agent,
                status=report.status,
                ended_at=self.ledger.now(),
                incomplete_reason=report.incomplete_reason,
                tasks_declared=report.tasks_declared,
                tasks_complete=report.tasks_complete,
                tasks_failed=report.tasks_failed,
                tasks_remaining=report.tasks_remaining,
                attempts=report.attempts,
                prompt_tokens=report.prompt_tokens,
                completion_tokens=report.completion_tokens,
                elapsed_s=report.elapsed_s,
            )
        )
        return report

    def _report(
        self, status: str, reason: str | None, elapsed: float, state: LedgerState
    ) -> RunReport:
        return RunReport(
            run_id=self.config.run_id,
            agent=self.config.agent,
            status=status,
            incomplete_reason=reason,
            tasks_declared=len(self.config.task_ids),
            tasks_complete=len(self._complete),
            tasks_failed=len(self._failed),
            tasks_remaining=len([t for t in self.config.task_ids if t not in self._complete]),
            attempts=self.attempts,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            elapsed_s=elapsed,
            resumed=state.exists,
            after_unclean_shutdown=state.unclean,
        )

    async def _drive(self, executor: TaskExecutor, remaining: Sequence[str]) -> None:
        if not remaining:
            return
        pending = iter(remaining)
        workers = min(self.config.concurrency, len(remaining))
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(workers):
                    group.create_task(self._worker(executor, pending))
        except BaseExceptionGroup as group_error:
            # A task group reports failures as a group. A caller who asked for a run and
            # got an operator's interrupt, or `AllPoolsExhausted`, should see that and not
            # a wrapper around it; sibling workers are cancelled by the group itself and
            # contribute nothing, so a lone member is the normal case.
            if len(group_error.exceptions) == 1:
                raise group_error.exceptions[0] from None
            raise

    async def _worker(self, executor: TaskExecutor, pending: Iterator[str]) -> None:
        # `next` on a shared iterator has no await in it, so two workers cannot take the
        # same task: an asyncio task only yields at an await point.
        while True:
            try:
                task_id = next(pending)
            except StopIteration:
                return
            await self._run_one(executor, task_id)

    async def _run_one(self, executor: TaskExecutor, task_id: str) -> None:
        context = TaskContext(self, task_id)
        started = self.clock.monotonic()
        try:
            result = await executor(context)
        except Exception as error:
            # Deliberately not BaseException: an operator's interrupt is not a task
            # failure, and recording it as one would let a killed run look finished.
            #
            # The class recorded is the client's own, never a second taxonomy. Anything
            # that is not a ClientError did not come from a provider, so it is named as
            # what it is and the exception type goes into `detail` rather than into a
            # field Phase 2.5 will count failure classes out of.
            if isinstance(error, ClientError):
                context.last_error_class = classify(error).reason
            elif context.last_error_class is None:
                context.last_error_class = EXECUTOR_ERROR
            self._write_task(
                context,
                FAILED,
                self.clock.monotonic() - started,
                {"exception": type(error).__name__},
            )
            return
        detail = result.detail if isinstance(result, TaskResult) else {}
        self._write_task(context, COMPLETE, self.clock.monotonic() - started, detail)
