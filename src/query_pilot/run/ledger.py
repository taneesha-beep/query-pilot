"""Append-only JSONL: what a run did, in the order it did it.

One file per run, `runs/<run-id>/ledger.jsonl`, gitignored. Every row carries a ``kind``
and they share one file deliberately: **ordering across kinds is the thing a later phase
needs.** Which quota wall shut which pool during which task is unreconstructable from four
separate files, and it is exactly the question Phase 6's capacity numbers ask.

Five kinds. ``run_start`` and ``run_end`` bracket one *segment* of a run — a run resumed
three times has three segments in one file. ``attempt`` is one call to a provider.
``task`` is that task's terminal outcome. ``wall`` is a refusal that named a quota,
folded in from 1.2's sink so that the run which first walks into Google's daily ceiling is
the run that measures it.

**Durability, and what it does and does not buy.** Every row is one ``write`` under
``O_APPEND``, then ``flush``, then ``os.fsync``, before the next attempt is dispatched.
Measured on this machine over 500 rows: fsync costs a median of 0.029 ms and a p95 of
0.044 ms, against 0.002 ms for a flush alone — around a twentieth of a millisecond per
row, against provider latencies of 0.5-0.9 s. It is free at this scale and a
kill-and-restart test proves nothing without it. What it buys is survival of the process
being killed, which is the case the acceptance test names. It is **not** a power-loss
guarantee on macOS, where ``fsync(2)`` does not flush the drive's own cache; that would
need ``F_FULLFSYNC``, and nothing here is worth the cost of it.

**One writing process per ledger.** The append does no ``await``, which makes it atomic
against other coroutines on one event loop by construction, and a lock covers the case of
a sink called from a thread. Two processes appending to one run is not supported and is
not something this project does.

**A row is written once, after its outcome is known.** There is no "attempt started" row,
so a half-written attempt cannot exist and a reader never has to guess what a dangling one
meant. The cost is stated rather than hidden: tokens spent by attempts in flight when a
process is killed are never recorded, and are never charged against the budget on resume.
That loss is bounded by the concurrency cap and is the same order as the token overshoot
1.2 already accepted, since a token count does not exist until the answer arrives. What
*is* recorded is the kill itself, at the run level: a clean exit writes ``run_end``, so
**a segment with no ``run_end`` was cut off**, and resume says so in the next
``run_start`` instead of pretending to know what the lost attempts cost.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any

from query_pilot.client.classify import classify
from query_pilot.client.clock import Clock, SystemClock
from query_pilot.client.errors import ClientError
from query_pilot.client.scheduler import QuotaWall
from query_pilot.client.types import Completion

#: Run output lives here, gitignored. Only derived summaries are ever committed.
DEFAULT_RUNS_ROOT = Path("runs")
LEDGER_NAME = "ledger.jsonl"

#: A task the run got an answer for. Whether the answer was *right* is not this layer's
#: question — Phase 2 records that in the row's ``detail`` — and the distinction is what
#: keeps a run ledger free of any notion of SQL.
COMPLETE = "complete"
#: A task the run failed to get an answer for, because infrastructure stopped it.
FAILED = "failed"

#: An attempt's two outcomes. Two, not more: *why* it failed is ``error_class``, which is
#: the client's own classifier speaking rather than a taxonomy invented here.
OK = "ok"
ERROR = "error"

#: The one label this package adds, and it is deliberately not a provider failure class:
#: it means the thing executing the task raised something that was never a provider's
#: doing. Filing a bug in the executor under a provider's name would corrupt the failure
#: distribution Phase 2.5 reads, so it gets its own word and the exception type goes into
#: the task row's ``detail`` where it belongs.
EXECUTOR_ERROR = "executor_error"


@dataclass(frozen=True, slots=True)
class AttemptRow:
    """One call to a provider, with everything needed to reconstruct what it cost.

    ``pool`` and ``model_returned`` are here although the roadmap's field list omits both,
    and neither is optional in practice. Buckets are per pool per model and Google runs two
    independent projects, so a run spanning both must be able to say which served what.
    And Google answers a request for a pinned model with its own dated build string: a row
    recording only what was asked for cannot tell that the served model changed underneath
    the run.

    ``error_class`` is the classifier's own ``reason`` — ``quota_day``, ``bot_block``,
    ``malformed_response`` and the rest. **There is no second taxonomy here.** A row that
    needs a class the classifier does not produce is a reason to change the classifier.
    """

    run_id: str
    task_id: str
    agent: str
    provider: str
    model: str
    pool: str
    outcome: str
    recorded_at: str
    turn: int = 1
    attempt: int = 1
    model_returned: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_s: float | None = None
    finish_reason: str | None = None
    error_class: str | None = None

    @property
    def total_tokens(self) -> int:
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)

    @classmethod
    def answered(
        cls,
        completion: Completion,
        *,
        run_id: str,
        task_id: str,
        agent: str,
        recorded_at: str,
        turn: int = 1,
        attempt: int = 1,
    ) -> AttemptRow:
        """Built from a :class:`Completion`, which already carries most of a row."""
        return cls(
            run_id=run_id,
            task_id=task_id,
            agent=agent,
            provider=completion.provider,
            model=completion.model,
            pool=completion.pool,
            outcome=OK,
            recorded_at=recorded_at,
            turn=turn,
            attempt=attempt,
            model_returned=completion.model_returned,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            latency_s=completion.latency_s,
            finish_reason=completion.finish_reason,
        )

    @classmethod
    def refused(
        cls,
        error: ClientError,
        *,
        run_id: str,
        task_id: str,
        agent: str,
        recorded_at: str,
        turn: int = 1,
        attempt: int = 1,
        provider: str | None = None,
        model: str | None = None,
        pool: str | None = None,
        latency_s: float | None = None,
    ) -> AttemptRow:
        """Built from whatever the client raised, classified by the client's own rule."""
        return cls(
            run_id=run_id,
            task_id=task_id,
            agent=agent,
            provider=provider if provider is not None else getattr(error, "provider", ""),
            model=model if model is not None else getattr(error, "model", ""),
            pool=pool if pool is not None else getattr(error, "pool", ""),
            outcome=ERROR,
            recorded_at=recorded_at,
            turn=turn,
            attempt=attempt,
            latency_s=latency_s,
            error_class=classify(error).reason,
        )

    def as_row(self) -> dict[str, Any]:
        return {"kind": "attempt", **asdict(self)}


@dataclass(frozen=True, slots=True)
class TaskRow:
    """One task's terminal outcome, and the row the resume rule reads.

    ``detail`` is free-form and this package never looks inside it. Phase 2 puts solved or
    unsolved there; nothing here needs to know the difference, and nothing here should.
    """

    run_id: str
    task_id: str
    agent: str
    status: str
    recorded_at: str
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_s: float = 0.0
    error_class: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {"kind": "task", **asdict(self)}


@dataclass(frozen=True, slots=True)
class RunStartRow:
    run_id: str
    agent: str
    fingerprint: str
    started_at: str
    declared: Mapping[str, Any] = field(default_factory=dict)
    tasks_declared: int = 0
    tasks_remaining: int = 0
    resumed: bool = False
    #: True when the previous segment has no ``run_end`` — the process was killed, and
    #: whatever was in flight was lost unrecorded.
    after_unclean_shutdown: bool = False
    previous_fingerprint: str | None = None

    def as_row(self) -> dict[str, Any]:
        return {"kind": "run_start", **asdict(self)}


@dataclass(frozen=True, slots=True)
class RunEndRow:
    """Written only on a clean exit. Its absence is what says a segment was cut off."""

    run_id: str
    agent: str
    status: str
    ended_at: str
    incomplete_reason: str | None = None
    tasks_declared: int = 0
    tasks_complete: int = 0
    tasks_failed: int = 0
    tasks_remaining: int = 0
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_s: float = 0.0

    def as_row(self) -> dict[str, Any]:
        return {"kind": "run_end", **asdict(self)}


class RunLedger:
    """The append-only file, and the only thing that writes to it."""

    def __init__(
        self,
        run_id: str,
        *,
        root: Path | str = DEFAULT_RUNS_ROOT,
        clock: Clock | None = None,
    ) -> None:
        self.run_id = run_id
        self.root = Path(root)
        self.clock = clock or SystemClock()
        self._handle: IO[str] | None = None
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        return self.root / self.run_id

    @property
    def path(self) -> Path:
        return self.directory / LEDGER_NAME

    def now(self) -> str:
        return self.clock.now_utc().isoformat()

    # -- writing -------------------------------------------------------------------------
    def open(self) -> RunLedger:
        if self._handle is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
        return self

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    def __enter__(self) -> RunLedger:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def append(self, row: Mapping[str, Any]) -> None:
        """One row, on disk before this returns.

        Opens on demand rather than requiring a caller to have called :meth:`open`: a
        quota-wall sink handed to a client can outlive the block that opened the ledger,
        and dropping the one refusal that names Google's daily ceiling would cost more than
        an extra ``open`` ever will.
        """
        line = json.dumps(row, default=str) + "\n"
        with self._lock:
            if self._handle is None:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._handle = self.path.open("a", encoding="utf-8")
            self._handle.write(line)
            self._handle.flush()
            os.fsync(self._handle.fileno())

    def record_start(self, row: RunStartRow) -> None:
        self.append(row.as_row())

    def record_attempt(self, row: AttemptRow) -> None:
        self.append(row.as_row())

    def record_task(self, row: TaskRow) -> None:
        self.append(row.as_row())

    def record_end(self, row: RunEndRow) -> None:
        self.append(row.as_row())

    def on_quota_wall(self, wall: QuotaWall) -> None:
        """The sink 1.2 left to be taken over. Pass this to ``Client.from_config``."""
        self.append({"kind": "wall", "run_id": self.run_id, **wall.as_row()})

    # -- reading -------------------------------------------------------------------------
    def read(self) -> LedgerState:
        return read_ledger(self.path)


@dataclass(frozen=True, slots=True)
class Segment:
    """One start-to-end stretch of a run. A run resumed twice has three."""

    started_at: str
    fingerprint: str | None = None
    ended_at: str | None = None
    status: str | None = None
    incomplete_reason: str | None = None

    @property
    def clean(self) -> bool:
        """False when the process was killed before it could write ``run_end``."""
        return self.ended_at is not None


@dataclass(frozen=True, slots=True)
class LedgerState:
    """What a ledger says has already happened, which is what resume reads."""

    run_id: str | None = None
    exists: bool = False
    complete_task_ids: frozenset[str] = frozenset()
    failed_task_ids: frozenset[str] = frozenset()
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    walls: int = 0
    segments: tuple[Segment, ...] = ()
    #: Lines that were not readable JSON. A process killed mid-``write`` can in principle
    #: leave a partial last line; it is counted and skipped, never a reason to refuse to
    #: resume, because refusing is how a killed run becomes unrecoverable.
    unreadable_lines: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def fingerprint(self) -> str | None:
        for segment in reversed(self.segments):
            if segment.fingerprint:
                return segment.fingerprint
        return None

    @property
    def unclean(self) -> bool:
        """The last segment has no ``run_end``: the process was killed."""
        return bool(self.segments) and not self.segments[-1].clean


def read_rows(path: Path | str) -> Iterator[dict[str, Any]]:
    """Every readable row, in file order. Unreadable lines are skipped."""
    source = Path(path)
    if not source.exists():
        return
    with source.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def read_ledger(path: Path | str) -> LedgerState:
    """Fold a ledger into what a resuming run needs to know.

    Later rows win: a task recorded ``failed`` and then retried to ``complete`` is
    complete, which is what makes retrying a failure safe.
    """
    source = Path(path)
    if not source.exists():
        return LedgerState()

    run_id: str | None = None
    status: dict[str, str] = {}
    attempts = prompt_tokens = completion_tokens = walls = unreadable = 0
    segments: list[Segment] = []

    with source.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                unreadable += 1
                continue
            if not isinstance(row, dict):
                unreadable += 1
                continue
            run_id = run_id or row.get("run_id")
            kind = row.get("kind")
            if kind == "attempt":
                attempts += 1
                prompt_tokens += row.get("prompt_tokens") or 0
                completion_tokens += row.get("completion_tokens") or 0
            elif kind == "task":
                task_id = row.get("task_id")
                if isinstance(task_id, str):
                    status[task_id] = str(row.get("status", FAILED))
            elif kind == "wall":
                walls += 1
            elif kind == "run_start":
                segments.append(
                    Segment(
                        started_at=str(row.get("started_at", "")),
                        fingerprint=row.get("fingerprint"),
                    )
                )
            elif kind == "run_end":
                ended = Segment(
                    started_at=segments[-1].started_at if segments else "",
                    fingerprint=segments[-1].fingerprint if segments else None,
                    ended_at=str(row.get("ended_at", "")),
                    status=row.get("status"),
                    incomplete_reason=row.get("incomplete_reason"),
                )
                if segments:
                    segments[-1] = ended
                else:
                    segments.append(ended)

    return LedgerState(
        run_id=run_id,
        exists=True,
        complete_task_ids=frozenset(t for t, s in status.items() if s == COMPLETE),
        failed_task_ids=frozenset(t for t, s in status.items() if s != COMPLETE),
        attempts=attempts,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        walls=walls,
        segments=tuple(segments),
        unreadable_lines=unreadable,
    )
