"""A0 — one prompt, one call, one statement, no second chance.

**This is the baseline everything else is measured against, so it is built to be a good
one.** A weak baseline manufactures a result in A1's favour and is the first thing a
competent interviewer attacks. Concretely, that means: the whole schema rather than a
guessed subset, read from the database rather than from a dataset annotation; a prompt that
says exactly what a correct answer looks like; and an extraction step that finds the SQL a
model wrote in every shape models write it in.

**A0 uses the `strong` role, and that is a decision rather than a default.** Phase 5.2 fixes
A1 as the always-strong arm, and the A0-against-A1 figure is only a statement about the
agent loop if the model is held constant across it. A0 on the cheap model would report the
loop's gain and the model's gain added together, under the loop's name. Phase 5 is where
the model is allowed to vary, and it is the only place.

**One call, no tools, no repair.** A tool layer is 3.1, a loop is 3.2 and repair is 3.3. If
this file ever makes a second request for one task, the comparison those phases exist to
make has quietly stopped being available.

What A0 does *not* do is let a provider decide the accuracy figure. A `ClientError` — a
quota wall, a refused pool — propagates, so the run loop records the task **failed** and a
resume retries it. Only an answer this project actually received is ever scored.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from query_pilot.agents.schema import Schema, read_schema, render_schema
from query_pilot.agents.sql import extract_sql
from query_pilot.client.client import Client
from query_pilot.client.types import Message
from query_pilot.equivalence import NO_SQL, Comparison, compare, orders_rows
from query_pilot.run.loop import TaskContext, TaskResult
from query_pilot.sandbox import CopyScope, Sandbox, SubstrateCopies
from query_pilot.tasks import Task

__all__ = [
    "A0",
    "ANSWER_RULES",
    "MAX_OUTPUT_TOKENS",
    "ROLE",
    "SYSTEM_PROMPT",
    "Attempt",
    "build_prompt",
]

#: Held constant against A1. See the module docstring.
ROLE: Final = "strong"

#: A ceiling on one answer, not a target.
#:
#: A policy choice, and here is what it rests on: the longest reference query in this
#: substrate is a few hundred characters, so a statement needs a small fraction of this.
#: The headroom is for models that narrate before answering. **A completion that stops at
#: this ceiling reports `finish_reason == "length"`**, which A0 records in the task's detail
#: — so if that ever appears in a run, the number is measured rather than guessed at and the
#: ceiling gets raised against evidence.
MAX_OUTPUT_TOKENS: Final = 1024

#: What a correct answer looks like, shared with A1 **verbatim**.
#:
#: Split out of :data:`SYSTEM_PROMPT` in 3.2 and shared rather than copied, because the
#: A0-against-A1 figure is a statement about the agent loop only if everything else is held
#: constant — and a second copy of these five lines is a second thing that can drift. The
#: concatenation below is pinned by a test to the exact string A0 was measured with, so
#: this refactor cannot have moved the prompt that produced 124 of 150.
ANSWER_RULES: Final = (
    "Rules:\n"
    "- Use only the tables and columns in the schema, spelled exactly as they appear.\n"
    "- Return exactly the columns the question asks for, in the order it asks for them, "
    "and no others.\n"
    "- Add ORDER BY only when the question asks for an order.\n"
    "- Add DISTINCT only when the question asks for distinct values.\n"
    "- Write one statement. Do not explain it, and do not end it with a semicolon."
)

SYSTEM_PROMPT: Final = (
    "You are an expert SQLite analyst. You are given the complete schema of one SQLite "
    "database and one question about it. Reply with a single SQLite SELECT statement that "
    "answers the question, and nothing else.\n"
    "\n" + ANSWER_RULES
)


def build_prompt(schema: Schema, question: str) -> list[Message]:
    """The single exchange A0 sends.

    The schema comes first and the question last, so that the part which varies per task
    sits at the end of the prompt — where an instruction is most reliably followed, and
    where a provider's prefix cache, if it has one, would keep the schema across the tasks
    that share a database.
    """
    return [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(
            role="user",
            content=(
                f"Database: {schema.db_id}\n\n"
                f"{render_schema(schema)}\n\n"
                f"Question: {question}\n\n"
                "SQLite query:"
            ),
        ),
    ]


@dataclass(frozen=True, slots=True)
class Attempt:
    """Everything one task produced, before it is flattened into a ledger row.

    Kept as its own type so a test can assert on the parts without reading a dictionary,
    and so :meth:`detail` is the only place that decides what reaches the ledger.
    """

    task: Task
    comparison: Comparison
    sql: str | None
    dropped_statements: int = 0
    fenced: bool = False
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    provider: str | None = None
    model: str | None = None
    latency_s: float | None = None
    candidate_seconds: float | None = None
    candidate_rows: int | None = None
    candidate_truncated_by: str | None = None
    candidate_timed_out: bool = False
    reference_rows: int | None = None

    def detail(self) -> dict[str, Any]:
        """What goes into the task row's free-form ``detail``, which `run/` never opens.

        `Comparison.as_detail()` first, because solved-or-not is what 2.4 reads; the rest is
        what 2.5 needs to read thirty failures by hand without re-running anything.
        """
        row: dict[str, Any] = dict(self.comparison.as_detail())
        row.update(
            {
                "db_id": self.task.db_id,
                "sql": self.sql,
                "reference_sql": self.task.reference_sql,
                "fenced": self.fenced,
                "dropped_statements": self.dropped_statements,
                "finish_reason": self.finish_reason,
                "provider": self.provider,
                "model": self.model,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "latency_s": self.latency_s,
                "candidate_rows": self.candidate_rows,
                "candidate_seconds": self.candidate_seconds,
                "candidate_truncated_by": self.candidate_truncated_by,
                "candidate_timed_out": self.candidate_timed_out,
                "reference_rows": self.reference_rows,
            }
        )
        return row


class A0:
    """The single-shot agent, shaped as a run's ``TaskExecutor``.

    Instances are callable, so ``Run(executor=A0(...))`` is the whole wiring. The run loop
    hands it a :class:`~query_pilot.run.loop.TaskContext` and nothing else, which is why the
    task list is held here: `run/` does not know what a task is and does not learn.
    """

    def __init__(
        self,
        client: Client,
        tasks: Iterable[Task],
        database_root: Path | str,
        *,
        role: str = ROLE,
        sandbox: Sandbox | None = None,
        copies: SubstrateCopies | None = None,
        copy_scope: CopyScope = CopyScope.RUN,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
    ) -> None:
        self.client = client
        self.tasks: Mapping[str, Task] = {task.task_id: task for task in tasks}
        self.role = role
        self.sandbox = sandbox or Sandbox()
        self.copies = copies or SubstrateCopies(database_root, scope=copy_scope)
        self.max_output_tokens = max_output_tokens
        self._schemas: dict[str, Schema] = {}
        self._schema_lock = threading.Lock()

    def close(self) -> None:
        """Remove the copies. A run that ends without this leaves a temporary directory."""
        self.copies.close()

    async def __call__(self, context: TaskContext) -> TaskResult:
        return TaskResult(detail=(await self.solve(context)).detail())

    async def solve(self, context: TaskContext) -> Attempt:
        """One task, end to end: schema, prompt, call, extract, execute, compare."""
        task = self.tasks[context.task_id]
        with self.copies.for_task(task.db_id, task_id=task.task_id) as database:
            # Every sqlite3 call goes through a thread. The deadline is 30 seconds and the
            # run's concurrency is 8, so a blocking execute on the event loop would stall
            # every other task in the run behind one bad query.
            schema = await asyncio.to_thread(self._schema, database, task.db_id)

            completion = await self.client.complete(
                self.role,
                build_prompt(schema, task.question),
                max_output_tokens=self.max_output_tokens,
            )
            context.record(completion)

            extraction = extract_sql(completion.text)
            shared = {
                "fenced": extraction.fenced,
                "dropped_statements": extraction.dropped,
                "finish_reason": completion.finish_reason,
                "prompt_tokens": completion.prompt_tokens,
                "completion_tokens": completion.completion_tokens,
                "provider": completion.provider,
                "model": completion.model,
                "latency_s": completion.latency_s,
            }
            if extraction.sql is None:
                return Attempt(
                    task=task,
                    comparison=Comparison(False, NO_SQL, extraction.reason or ""),
                    sql=None,
                    **shared,
                )

            candidate = await asyncio.to_thread(self.sandbox.execute, database, extraction.sql)
            reference = await asyncio.to_thread(self.sandbox.execute, database, task.reference_sql)
            comparison = compare(
                reference.rows if reference.ok else None,
                candidate.rows if candidate.ok else None,
                ordered=orders_rows(task.reference_sql),
                reference_error=reference.error,
                candidate_error=candidate.error,
                reference_truncated=reference.truncated,
                candidate_truncated=candidate.truncated,
            )
            return Attempt(
                task=task,
                comparison=comparison,
                sql=extraction.sql,
                candidate_seconds=candidate.elapsed_s,
                candidate_rows=len(candidate.rows) if candidate.ok else None,
                candidate_truncated_by=candidate.truncated_by,
                candidate_timed_out=candidate.timed_out,
                reference_rows=len(reference.rows) if reference.ok else None,
                **shared,
            )

    def _schema(self, database: Path, db_id: str) -> Schema:
        """Read once per database, not once per task.

        The schema does not depend on which copy it is read from, so this cache is correct
        under both copy scopes. Locked because the run calls it from several threads.
        """
        with self._schema_lock:
            cached = self._schemas.get(db_id)
            if cached is not None:
                return cached
        schema = read_schema(self.sandbox, database, db_id)
        with self._schema_lock:
            return self._schemas.setdefault(db_id, schema)
