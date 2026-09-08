"""A1 — the agent loop: several turns, four tools, one transcript, five ways to stop.

**A1 is A0 plus the loop, and nothing else.** Same `strong` role, same model, same output
ceiling, same answer rules word for word (`a0.ANSWER_RULES`), same sandbox, same
equivalence rule, and the final SQL is re-executed and compared exactly as A0's is. Every
one of those is held constant deliberately: the A0-against-A1 figure is a statement about
the agent loop only if the loop is the only thing that moved, and constraint 40 keeps the
model out of it — Phase 5 is the one place the model is allowed to vary.

**The one difference is the schema.** A0 is handed the whole database rendered into its
prompt. A1 is handed nothing and has to discover it, which is what the four tools are for
and what the extra turns are spent on.

**Termination: five paths, and the roadmap names four.** ``answer``, ``turn_limit``,
``tool_call_limit`` and ``budget`` are 3.2's. The fifth, ``prompt_ceiling``, is not a policy
choice and is stated as a deviation rather than slipped in: **no pair of policy limits can
stop a pathological trajectory putting one attempt over the endpoint's per-minute token
ceiling**, and constraint 46 is the finding that doing so does not fail the task — it ends
the whole run as ``pools_exhausted`` with no provider having refused anything. A limit whose
violation costs one task is a policy question; one whose violation costs 150 is enforcement,
and this is the enforcement.

**Which outcomes are a failed task, and it decides what a resumed run re-runs.** 1.3 retries
failed tasks and skips complete ones. The first four paths are **complete**: the run got
answers and paid for every turn, and constraint 25 says complete means the run got an
answer, whatever the answer was. Re-running a turn-limit task would spend the whole
trajectory again to hit the same wall. ``budget`` is **failed**, because the run stopped
rather than the task answering — the same rule as a quota wall in 2.3, and for the same
reason: a run-level stop must never put non-solves into the accuracy figure.

**Where a tool error goes, and where it does not.** An error the model can act on goes back
to the model as a tool result and the loop continues — bad SQL, an unknown table, a wrong
argument, an unknown tool name. That is the loop doing the one thing A0 structurally cannot,
and 3.4's recovery rate is counted out of exactly it. A :class:`ClientError` propagates
untouched, so the run loop records the task **failed** and a resume retries it. And this
project's own faults — a substrate that will not open — propagate too, which 1.3 files as
``executor_error``. The line: *an error the model can act on goes back to the model; an
error about whether this project could ask the model at all does not.*

**A turn's tool calls run sequentially, in the order the provider returned them.** The
tools are sqlite against a local copy; running them concurrently buys nothing measurable and
costs the one ordering guarantee 3.4, 4.3 and 7.2 all read the transcript for.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from query_pilot.agents.a0 import ANSWER_RULES, MAX_OUTPUT_TOKENS, ROLE
from query_pilot.agents.sql import extract_sql
from query_pilot.agents.tools import TOOL_SCHEMAS, call_tool
from query_pilot.agents.transcript import TranscriptWriter
from query_pilot.client.client import Client
from query_pilot.client.types import Completion, Message, ToolCall
from query_pilot.equivalence import NO_SQL, Comparison, compare, orders_rows
from query_pilot.run.guard import IncompleteReason
from query_pilot.run.loop import TaskContext, TaskResult
from query_pilot.sandbox import CopyScope, Sandbox, SubstrateCopies
from query_pilot.tasks import Task

__all__ = [
    "A1",
    "ANSWER",
    "BUDGET",
    "MAX_OUTPUT_TOKENS",
    "PROMPT_CEILING",
    "PROMPT_CEILING_CHARS",
    "ROLE",
    "SYSTEM_PROMPT",
    "TERMINATIONS",
    "TOOL_CALL_LIMIT",
    "TOOL_CALL_LIMIT_REACHED",
    "TURN_LIMIT",
    "TURN_LIMIT_REACHED",
    "BudgetStopped",
    "Trajectory",
    "build_prompt",
    "conversation_chars",
]

# --- the limits -----------------------------------------------------------------------------

#: How many requests one task may make. **A policy choice, and here is the derivation.**
#:
#: Two ends. The **floor** is what a legitimate trajectory needs, and the measured fact that
#: fixes it is in `docs/a1-tool-probe.json`: `openai/gpt-oss-120b` emitted **exactly one
#: tool call per turn, in 9 turns of 9**, on 2026-09-08. It does not batch, so a turn is a
#: tool call and the limit has to absorb every one of them — `list_tables`, one
#: `describe_table` per table the model decides it needs, a `sample_rows` or two, an
#: `execute_sql`, a repair after an error, and the answer turn. That is
#: :data:`TOOL_CALL_LIMIT` plus the turn that answers, plus one turn of slack for a model
#: that thinks aloud without calling anything.
#:
#: The **ceiling** is cost. The same probe measured prompt growth at **+50 to +107 tokens a
#: turn** (466 -> 516 -> 601), because the model describes only the tables it needs rather
#: than all of them, so fourteen turns is a last prompt near 1,900 tokens — comfortably
#: inside :data:`PROMPT_CEILING_CHARS`, which enforces the real wall anyway.
#:
#: **What it costs if it is wrong in either direction, stated because it is measurable at
#: 3.6:** too low and a legitimate trajectory is cut off mid-work, which lands as `no_sql`
#: and moves the accuracy figure down for a reason that is not the model's; too high and
#: 3.6 costs more Groq-days than it has to. The transcript records the outcome of every
#: trajectory, so 3.6 says how many hit it rather than leaving it to be assumed.
TURN_LIMIT: Final = 14

#: How many tool calls one task may make, across every turn. **A policy choice.**
#:
#: Independent of :data:`TURN_LIMIT` and needed beside it, because neither bounds the other:
#: one message can carry many tool calls (this model does not, but the format allows it and
#: a future model will), and a model can spend turns talking without calling anything.
#:
#: Derived from the working set's shape. The largest schema it touches has **11 tables**, and
#: the probe shows the model describes only the ones it needs — one, in both trajectories
#: that got that far. Twelve admits `list_tables` plus four `describe_table` plus two
#: `sample_rows` plus **five** `execute_sql`, which is a query, an error, a repair, a second
#: error and a second repair. **The recovery cycle is what this number is really protecting**:
#: recovery rate is the single most interesting figure in Phase 3 and a tool-call limit that
#: cut the second repair would cap the thing 3.4 exists to measure.
TOOL_CALL_LIMIT: Final = 12

#: The largest conversation, in characters, that may be sent as one request.
#:
#: **Not a policy choice. This is the enforcement of constraint 46**, and it is the fifth
#: termination path the roadmap does not name. Tokens are charged to the per-minute bucket
#: when an answer arrives; a deficit deeper than `wait_ceiling_s` of refill makes the client
#: raise `AllPoolsExhausted`, which `fatal_reason` treats as fatal, so **the run ends itself
#: with no provider having refused anything**. That is what nearly ended 2.4 at task 91 of
#: 150, and A1 is exposed to it in a way A0 was not: A0's attempts were a flat ~712 tokens,
#: and A1's last attempt carries every tool result of the trajectory.
#:
#: `(tpm - max_output_tokens) x chars_per_prompt_token` = `(8000 - 1024) x 3.265` = **22,776**,
#: with tpm from `config/providers.toml` at the `strong` role, the ratio measured in
#: `docs/a0-prompt-sizes.json` against ten real attempts, and the whole arithmetic committed
#: in `docs/a1-tool-sizes.json` where a test reads it back.
#:
#: **Why no choice of the two limits above can replace it.** `TOOL_CALL_LIMIT` calls, each
#: returning up to `tools.TOOL_RESULT_CHARS`, is 72,000 characters — three times this. To
#: make the product safe, either the tool-call limit falls to five, which caps the recovery
#: cycle 3.4 measures, or the result backstop falls below the largest legitimate tool result,
#: which cuts real answers. Both are worse than measuring the conversation, which is free.
PROMPT_CEILING_CHARS: Final = 22_776

# --- how a trajectory ends --------------------------------------------------------------------

#: The model stopped calling tools. Whatever it said is the answer, and it is scored the way
#: A0's single response is — **including scoring `no_sql` when it holds none.** Looking back
#: to the last `execute_sql` the model ran instead would rescue trajectories that never
#: committed to an answer, and would put a thumb on the scale A0 has no equivalent of.
ANSWER: Final = "answer"
TURN_LIMIT_REACHED: Final = "turn_limit"
TOOL_CALL_LIMIT_REACHED: Final = "tool_call_limit"
PROMPT_CEILING: Final = "prompt_ceiling"
#: The run's budget guard crossed a ceiling mid-trajectory. **The only one that fails the
#: task**, because the run stopped rather than the task answering.
BUDGET: Final = "budget"

TERMINATIONS: Final = (
    ANSWER,
    TURN_LIMIT_REACHED,
    TOOL_CALL_LIMIT_REACHED,
    PROMPT_CEILING,
    BUDGET,
)

SYSTEM_PROMPT: Final = (
    "You are an expert SQLite analyst answering one question about one SQLite database.\n"
    "\n"
    "You have NOT been shown the schema. Discover it with the tools:\n"
    "- list_tables to see what tables exist and how large they are;\n"
    "- describe_table for the columns, types and keys of a table you intend to use;\n"
    "- sample_rows when you need to see how a value is actually spelled before comparing "
    "against it;\n"
    "- execute_sql to run a query and see its rows, or the error it raised.\n"
    "\n"
    "Call one tool at a time and use what it returns. When a query fails, read the error "
    "and fix it. When you are ready to answer, stop calling tools and reply with the "
    "statement alone.\n"
    "\n" + ANSWER_RULES
)


class BudgetStopped(Exception):
    """The run's budget guard crossed a ceiling part-way through a trajectory.

    Raised so the run loop records the task **failed** and a resume retries it. It is
    deliberately not a :class:`~query_pilot.client.errors.ClientError`: no provider refused
    anything, this project stopped itself, and `fatal_reason` correctly declines to classify
    it — the guard has already been told to stop by :meth:`TaskContext.budget_stop`.
    """

    def __init__(self, reason: IncompleteReason) -> None:
        super().__init__(f"the run's budget guard stopped this trajectory: {reason.value}")
        self.reason = reason


def build_prompt(db_id: str, question: str) -> list[Message]:
    """The opening exchange. **No schema in it — that is the whole point of A1.**"""
    return [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content=f"Database: {db_id}\n\nQuestion: {question}"),
    ]


def conversation_chars(messages: Sequence[Message]) -> int:
    """What one request would cost, in the units :data:`PROMPT_CEILING_CHARS` is measured in.

    Counts the tool calls as the JSON they are serialised into, not just their text: a turn
    whose assistant message is empty still carries an `execute_sql` argument that can be
    hundreds of characters, and a count that ignored it would under-read the request that is
    about to be sent by exactly the amount that grows fastest.
    """
    total = 0
    for message in messages:
        total += len(message.content)
        for call in message.tool_calls:
            total += len(call.name) + len(json.dumps(dict(call.arguments), default=str))
    return total


@dataclass(frozen=True, slots=True)
class Trajectory:
    """Everything one task produced, before it is flattened into a ledger row.

    The same shape as A0's :class:`~query_pilot.agents.a0.Attempt` and for the same reason —
    so a test can assert on the parts, and so :meth:`detail` is the only place that decides
    what reaches the ledger. Every key A0 records is recorded here too, so that
    `agents/results.py` projects both agents without learning which one it is reading.

    The token counts are **sums over the trajectory** and the provider, model and finish
    reason are the **last turn's**, which is the turn that produced the answer being scored.
    """

    task: Task
    comparison: Comparison
    sql: str | None
    termination: str
    turns: int = 0
    tool_calls: int = 0
    tool_calls_by_name: Mapping[str, int] = field(default_factory=dict)
    transcript: str | None = None
    dropped_statements: int = 0
    fenced: bool = False
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider: str | None = None
    model: str | None = None
    latency_s: float | None = None
    candidate_seconds: float | None = None
    candidate_rows: int | None = None
    candidate_truncated_by: str | None = None
    candidate_timed_out: bool = False
    reference_rows: int | None = None

    def detail(self) -> dict[str, Any]:
        """What goes into the task row's free-form ``detail``, which `run/` never opens."""
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
                # A1's own, and the two the roadmap requires recorded per task.
                "termination": self.termination,
                "turns": self.turns,
                "tool_calls": self.tool_calls,
                "tool_calls_by_name": dict(self.tool_calls_by_name),
                "turn_limit": TURN_LIMIT,
                "tool_call_limit": TOOL_CALL_LIMIT,
                # Relative to the run directory, so a run directory stays movable.
                "transcript": self.transcript,
            }
        )
        return row


class A1:
    """The agent loop, shaped as a run's ``TaskExecutor`` — the same wiring A0 has.

    ``Run(config, ledger).execute(A1(...))`` is the whole of it. The run loop hands it a
    :class:`~query_pilot.run.loop.TaskContext` and nothing else, which is why the task list
    is held here: `run/` does not know what a task is and does not learn.
    """

    def __init__(
        self,
        client: Client,
        tasks: Iterable[Task],
        database_root: Path | str,
        run_directory: Path | str,
        *,
        role: str = ROLE,
        sandbox: Sandbox | None = None,
        copies: SubstrateCopies | None = None,
        copy_scope: CopyScope = CopyScope.RUN,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
        turn_limit: int = TURN_LIMIT,
        tool_call_limit: int = TOOL_CALL_LIMIT,
        prompt_ceiling_chars: int = PROMPT_CEILING_CHARS,
    ) -> None:
        if turn_limit < 2:
            # One turn cannot both call a tool and answer, so a trajectory with fewer than
            # two turns can never terminate on `answer` and A1 would be A0 with extra steps.
            raise ValueError("a turn limit below 2 cannot produce an answer after a tool call")
        if tool_call_limit < 1:
            raise ValueError("a tool call limit below 1 leaves A1 no way to see the schema")
        if prompt_ceiling_chars <= len(SYSTEM_PROMPT):
            # A ceiling under the opening prompt admits no trajectory at all. The loop
            # already refuses to let it stop the first turn, so this would be a ceiling that
            # ends every task after one shot -- a broken declaration, not a bounded run.
            raise ValueError("a prompt ceiling below the system prompt admits no trajectory")
        self.client = client
        self.tasks: Mapping[str, Task] = {task.task_id: task for task in tasks}
        self.run_directory = Path(run_directory)
        self.role = role
        self.sandbox = sandbox or Sandbox()
        self.copies = copies or SubstrateCopies(database_root, scope=copy_scope)
        self.max_output_tokens = max_output_tokens
        self.turn_limit = turn_limit
        self.tool_call_limit = tool_call_limit
        self.prompt_ceiling_chars = prompt_ceiling_chars
        self._lock = threading.Lock()

    def close(self) -> None:
        """Remove the copies. A run that ends without this leaves a temporary directory."""
        self.copies.close()

    async def __call__(self, context: TaskContext) -> TaskResult:
        return TaskResult(detail=(await self.solve(context)).detail())

    # -- the loop ---------------------------------------------------------------------------

    async def solve(self, context: TaskContext) -> Trajectory:
        """One task, end to end: discover, query, answer, execute, compare."""
        task = self.tasks[context.task_id]
        writer = TranscriptWriter(self.run_directory, task.task_id)
        messages = build_prompt(task.db_id, task.question)
        state = _State()
        with self.copies.for_task(task.db_id, task_id=task.task_id) as database:
            try:
                writer.start(
                    run_id=context.run_id,
                    agent=context.agent,
                    db_id=task.db_id,
                    question=task.question,
                    limits={
                        "turn_limit": self.turn_limit,
                        "tool_call_limit": self.tool_call_limit,
                        "prompt_ceiling_chars": self.prompt_ceiling_chars,
                    },
                    started_at=context.run.ledger.now(),
                )
                for message in messages:
                    writer.message(message, turn=0)
                await self._drive(context, writer, database, messages, state)
                writer.end(
                    outcome=state.termination,
                    turns=state.turns,
                    tool_calls=state.tool_calls,
                    ended_at=context.run.ledger.now(),
                )
            except BaseException as error:
                # A trajectory that did not get to finish still says how it ended. The one
                # thing a reader must never have to guess is whether a short transcript is a
                # short trajectory or a truncated file.
                writer.end(
                    outcome=BUDGET if isinstance(error, BudgetStopped) else "error",
                    turns=state.turns,
                    tool_calls=state.tool_calls,
                    ended_at=context.run.ledger.now(),
                )
                raise
            finally:
                writer.close()
            return await self._score(task, state, database, writer)

    async def _drive(
        self,
        context: TaskContext,
        writer: TranscriptWriter,
        database: Path,
        messages: list[Message],
        state: _State,
    ) -> _State:
        """Turns, until one of the four completing paths ends it or the budget stops it."""
        while True:
            reason = context.budget_stop()
            if reason is not None:
                # Between turns, never mid-turn: an answer already paid for is worth
                # recording, and a ceiling crossed at turn two must not buy twelve more.
                raise BudgetStopped(reason)
            if state.turns >= self.turn_limit:
                return state.stop(TURN_LIMIT_REACHED)
            if state.turns and conversation_chars(messages) > self.prompt_ceiling_chars:
                # Checked before the request rather than after, because the request is the
                # thing that would end the run. **It never stops the first turn**: a
                # trajectory that made no request at all would record `no_sql` for a reason
                # that is this project's configuration rather than the model, and at worst
                # A1 should degrade to one shot without a schema, not to nothing.
                return state.stop(PROMPT_CEILING)

            state.turns += 1
            completion = await self.client.complete(
                self.role,
                messages,
                TOOL_SCHEMAS,
                max_output_tokens=self.max_output_tokens,
            )
            context.record(completion, turn=state.turns)
            state.observe(completion)
            answer = Message(
                role="assistant", content=completion.text, tool_calls=completion.tool_calls
            )
            messages.append(answer)
            writer.message(answer, turn=state.turns)

            if not completion.tool_calls:
                state.text = completion.text
                return state.stop(ANSWER)
            if state.turns >= self.turn_limit:
                # No turn left to feed these results back into, so they are not run. The
                # transcript keeps the assistant message with its unexecuted calls, which is
                # what a trajectory that ended mid-reach actually looks like.
                return state.stop(TURN_LIMIT_REACHED)

            for call in completion.tool_calls:
                if state.tool_calls >= self.tool_call_limit:
                    return state.stop(TOOL_CALL_LIMIT_REACHED)
                self._run_tool(writer, database, messages, call, state)

    def _run_tool(
        self,
        writer: TranscriptWriter,
        database: Path,
        messages: list[Message],
        call: ToolCall,
        state: _State,
    ) -> None:
        """One call, executed and answered. **Every error here goes back to the model.**"""
        state.tool_calls += 1
        state.by_name[call.name] = state.by_name.get(call.name, 0) + 1
        with self._lock:
            # sqlite3 objects are not shared across threads here — the sandbox opens a
            # connection per call — but the run may drive several tasks at once and the
            # copies are shared under CopyScope.RUN.
            result = call_tool(self.sandbox, database, call)
        answer = Message(role="tool", content=result.content, tool_call_id=call.id, name=call.name)
        messages.append(answer)
        writer.message(answer, turn=state.turns)
        writer.tool_result(
            turn=state.turns,
            call_id=call.id,
            name=call.name,
            ok=result.ok,
            error=result.error,
            rows_returned=result.rows_returned,
            rows_shown=result.rows_shown,
            truncated_by=result.truncated_by,
            elapsed_s=result.elapsed_s,
        )

    # -- scoring, which is A0's, unchanged -----------------------------------------------------

    async def _score(
        self, task: Task, state: _State, database: Path, writer: TranscriptWriter
    ) -> Trajectory:
        """Execute the final SQL and compare it, **exactly as A0 does**.

        The result the model was shown by ``execute_sql`` is never what is scored. The final
        statement is run again through the same sandbox and put through the same equivalence
        rule, which is what makes the two agents' numbers the same kind of number and what
        keeps every rendering cap in `tools.py` on the prompt side of the run.
        """
        extraction = extract_sql(state.text)
        shared: dict[str, Any] = {
            "termination": state.termination,
            "turns": state.turns,
            "tool_calls": state.tool_calls,
            "tool_calls_by_name": dict(state.by_name),
            "transcript": str(writer.path.relative_to(self.run_directory)),
            "fenced": extraction.fenced,
            "dropped_statements": extraction.dropped,
            "finish_reason": state.finish_reason,
            "prompt_tokens": state.prompt_tokens,
            "completion_tokens": state.completion_tokens,
            "provider": state.provider,
            "model": state.model,
            "latency_s": state.latency_s,
        }
        if extraction.sql is None:
            return Trajectory(
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
        return Trajectory(
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


@dataclass
class _State:
    """What one trajectory has spent and what the last turn said.

    Private and mutable, unlike everything else in this package: it is the loop's own
    bookkeeping between turns, and the frozen :class:`Trajectory` is what leaves.
    """

    turns: int = 0
    tool_calls: int = 0
    by_name: dict[str, int] = field(default_factory=dict)
    text: str = ""
    termination: str = ANSWER
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider: str | None = None
    model: str | None = None
    finish_reason: str | None = None
    latency_s: float | None = None

    def observe(self, completion: Completion) -> None:
        """Tokens are summed over the trajectory; the rest is the last turn's."""
        self.prompt_tokens += completion.prompt_tokens or 0
        self.completion_tokens += completion.completion_tokens or 0
        self.latency_s = (self.latency_s or 0.0) + completion.latency_s
        self.provider = completion.provider
        self.model = completion.model
        self.finish_reason = completion.finish_reason

    def stop(self, termination: str) -> _State:
        self.termination = termination
        return self
