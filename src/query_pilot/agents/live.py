"""A question someone typed, 7.1: A1's loop unchanged, with no reference and no verdict.

**A typed question has no reference query, so it is never scored.** A1 scores every trajectory
by executing its final statement and the task's reference and comparing the two; a question
typed into the local API has nothing to compare against. :class:`LiveA1` is :class:`A1` with
that one step replaced: the loop, its limits, its repair and its transcript are A1's own, and
``A1`` itself is not touched, so nothing that produced a committed figure can move.

**The final statement runs on the untrusted surface.** A1's scoring path is deliberately
unguarded (constraint 81) — nothing may re-score a committed run, and 3.6 ran on the
four-control sandbox. A typed question is exactly the input constraint 81 guards against, so
here the final statement goes through ``tools.execute_sql``: the guarded path, where controls 4
and 5 fire before a connection opens, rendered under the same row and character caps the
agent is shown. What came back is the outcome record, in the task row's ``detail``.

**Any answer here is a new draw.** It is recorded like every other trajectory — a run of one
task, a ledger, a transcript — under `runs/api/`, and no committed figure reads it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from query_pilot.agents.a1 import A1, FAIL_TASK, SCORE_UNSOLVED, TOOL_CALL_LIMIT, TURN_LIMIT
from query_pilot.agents.tools import execute_sql
from query_pilot.agents.transcript import TranscriptWriter
from query_pilot.agents.validate import Validation
from query_pilot.tasks import Task

__all__ = ["AGENTS", "LIVE_TASK_ID", "LiveA1", "LiveAgent", "LiveTrajectory", "live_task"]

#: The one task in a live run. Never a working-set ID (`dev-NNNN`) or an attack case ID, so a
#: live trajectory cannot be mistaken for one of theirs by anything that reads task IDs.
LIVE_TASK_ID: Final = "question"


@dataclass(frozen=True, slots=True)
class LiveAgent:
    """Which role a live question runs on, and what a refused generation does to it."""

    role: str
    rejected_generation: str


#: The two agents the API offers, each declared exactly as its measured run was.
#:
#: - **A1** on `strong`, under `fail_task` — 3.6's declaration.
#: - **A2-cheap** — A1's loop on the cheap model — on `cheap-no-spillover` under
#:   `score_unsolved`, as constraint 89 requires of every cheap run. Never on `cheap`, which
#:   spills to a Google model A1's tool-calling path has never run on.
#:
#: A2-cheap is here for 7.4's clip: a repair is what the clip shows, and 3.6's run repaired 1
#: trajectory of 150 on the strong model where A2-cheap's repaired 30.
AGENTS: Final[Mapping[str, LiveAgent]] = {
    "A1": LiveAgent(role="strong", rejected_generation=FAIL_TASK),
    "A2-cheap": LiveAgent(role="cheap-no-spillover", rejected_generation=SCORE_UNSOLVED),
}


def live_task(db_id: str, question: str) -> Task:
    """A task with no reference. The empty reference is never executed: see :class:`LiveA1`."""
    return Task(task_id=LIVE_TASK_ID, db_id=db_id, question=question, reference_sql="")


@dataclass(frozen=True, slots=True)
class LiveTrajectory:
    """What a live question produced. :class:`~query_pilot.agents.a1.Trajectory`'s shape,
    less everything that needs a reference: no comparison, no reason slug, no verdict."""

    task: Task
    sql: str | None
    validation: Validation
    termination: str
    turns: int
    tool_calls: int
    tool_calls_by_name: Mapping[str, int]
    transcript: str
    repair_attempts: int
    repair_succeeded: bool
    repair_blocked: str | None
    finish_reason: str | None
    prompt_tokens: int
    completion_tokens: int
    provider: str | None
    model: str | None
    latency_s: float | None
    #: What running the final statement through the guarded path returned, or ``None`` when
    #: there was no statement to run.
    result: Mapping[str, Any] | None = None
    provider_rejection: Mapping[str, Any] | None = field(default=None)

    def detail(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "scored": False,
            "db_id": self.task.db_id,
            "sql": self.sql,
            "validation_rule": self.validation.rule,
            "validation_reason": self.validation.reason,
            "fenced": self.validation.fenced,
            "dropped_statements": self.validation.dropped,
            "result": dict(self.result) if self.result is not None else None,
            "finish_reason": self.finish_reason,
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_s": self.latency_s,
            "termination": self.termination,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "tool_calls_by_name": dict(self.tool_calls_by_name),
            "turn_limit": TURN_LIMIT,
            "tool_call_limit": TOOL_CALL_LIMIT,
            "repair_attempts": self.repair_attempts,
            "repair_succeeded": self.repair_succeeded,
            "repair_blocked": self.repair_blocked,
            "transcript": self.transcript,
        }
        if self.provider_rejection is not None:
            row["provider_rejection"] = dict(self.provider_rejection)
        return row


class LiveA1(A1):
    """A1, with scoring replaced by one guarded execution of the final statement.

    Only :meth:`_score` is overridden. Everything before it — turns, tools, limits, the repair,
    the transcript bracket — is the measured loop, which is what makes a live trajectory a
    trajectory of the agent the committed figures describe rather than of a lookalike.
    """

    async def _score(  # type: ignore[override]
        self,
        task: Task,
        state: Any,
        validation: Validation,
        database: Path,
        writer: TranscriptWriter,
    ) -> LiveTrajectory:
        result: dict[str, Any] | None = None
        if validation.sql is not None:
            ran = await asyncio.to_thread(
                execute_sql, self.sandbox, database, {"sql": validation.sql}
            )
            result = {
                "ok": ran.ok,
                "error": ran.error,
                "text": ran.content if ran.ok else None,
                "rows_returned": ran.rows_returned,
                "rows_shown": ran.rows_shown,
                "truncated_by": ran.truncated_by,
                "elapsed_s": ran.elapsed_s,
            }
        rejection = None
        if state.rejection is not None:
            rejection = {key: state.rejection[key] for key in ("status", "code", "message")}
        return LiveTrajectory(
            task=task,
            sql=validation.sql,
            validation=validation,
            termination=state.termination,
            turns=state.turns,
            tool_calls=state.tool_calls,
            tool_calls_by_name=dict(state.by_name),
            transcript=str(writer.path.relative_to(self.run_directory)),
            repair_attempts=state.repair_attempts,
            repair_succeeded=state.repair_succeeded,
            repair_blocked=state.repair_blocked,
            finish_reason=state.finish_reason,
            prompt_tokens=state.prompt_tokens,
            completion_tokens=state.completion_tokens,
            provider=state.provider,
            model=state.model,
            latency_s=state.latency_s,
            result=result,
            provider_rejection=rejection,
        )
