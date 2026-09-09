"""3.4's four trajectory metrics, computed from a run's transcripts and its ledger.

**These are what A1 has to say for itself that A0 cannot.** A0 makes one request and either
solves the task or does not; there is no trajectory to measure, so recovery, waste and turns
have no A0 column and never will. 3.6 publishes all four and 4.3 quotes a rate beside them,
which is why every definition below is written down rather than left in the code.

**The transcript is the content record and the ledger is the outcome record.** Whether a task
solved is not in the transcript and must not be — constraint 51 — so this reads a **run
directory** and joins ``transcripts/<task_id>.jsonl`` against ``ledger.jsonl`` on
``task_id``. Nothing here needed a new transcript field, which was checked before 3.6 rather
than after.

**A task run twice keeps its last trajectory**, the same rule `transcript.Trajectory`
documents and the same rule the ledger's own rows follow: the earlier attempt is evidence,
not a second measurement.

---

**tool calls per task** — the number of ``tool_result`` events, which is **not** the number
of tool calls the assistant messages asked for. A trajectory stopped at the turn limit keeps
an assistant message whose calls were never executed, because there was no turn left to feed
them back into; counting those would report work that did not happen. Mean, median and p90
over every trajectory read.

**turns to solve** — the number of assistant messages, over **solved tasks only**. A repair
turn counts, because it is a request the run paid for. Derived from the events rather than
taken from the ``end`` event's own count, so that the count is checkable against it — a
trajectory whose two disagree is reported rather than silently preferred.

**recovery rate** — **three numbers, because one denominator would hide a real ambiguity.**
The roadmap's definition is "of tasks where the first ``execute_sql`` errored or returned
empty, the share that went on to solve", and *returned empty* is doing more work in that
sentence than it looks:

- ``error`` — the first ``execute_sql`` came back ``ok=false``. **This is the headline.** The
  model wrote SQL that did not run, was shown the error, and went on to solve: unambiguous,
  and exactly the capability A0 structurally lacks.
- ``empty`` — the first ``execute_sql`` succeeded and returned no rows. Reported apart
  because **an empty result can be the correct answer**: `docs/EQUIVALENCE.md` prices a query
  returning nothing at 4.7389% of the frame, over the 49 of 1,034 references that
  legitimately return no rows. This denominator therefore contains tasks that had nothing to
  recover from, and pooling it into the headline would inflate the number Phase 3 is about.
- ``error_or_empty`` — the roadmap's literal union, reported beside both so that neither
  reading is hidden.

**A task with no ``execute_sql`` at all is in none of these denominators** — it has no first
one — and is counted separately so the three rates can be read against how many tasks were
eligible at all. 4.3's compliance and containment rates have different denominators again and
must never be quoted as one figure with these; naming all of them is what keeps that honest.

**wasted-call rate** — tool calls made **after the last call that contributed to the final
query**. *Contributed* is defined here, precisely, and the definition is **approximate**:

- ``execute_sql`` contributes when its ``sql`` argument, normalised, **equals the final
  answer**. Whitespace collapsed, case folded, a trailing semicolon dropped. The strongest
  signal available: the model ran exactly the query it then gave.
- ``describe_table`` and ``sample_rows`` contribute when the table they name appears in the
  final query's identifier tokens, read from the query with literals and comments blanked so
  that a name inside a string is not a match.
- ``list_tables`` contributes whenever the final query names any table. It is what made every
  table name knowable, and `docs/a1-tool-probe.json` has it called first in 3 trajectories
  of 3.

**Where the approximation is, stated beside the number rather than left to be found:** a
``describe_table`` on a table the final query never mentions counts as wasted **even when it
was the call that ruled that table out**. Ruling a table out is real work and this metric
cannot see it. **The figure is therefore an upper bound on waste, not a measurement of it**,
and it is worth reporting anyway because it is reproducible from the transcript alone and
because the direction it moves between two agents is meaningful even when its level is not.

Two exclusions, each counted rather than folded in: a task whose final answer holds no SQL
has nothing to have contributed to, and a task that made no tool calls has no calls to waste.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from query_pilot.agents.sql import blank_literals
from query_pilot.agents.transcript import MESSAGE, TOOL_RESULT, TRANSCRIPTS_DIR, read_trajectories
from query_pilot.agents.validate import validate_answer
from query_pilot.run.ledger import COMPLETE, LEDGER_NAME, read_rows

__all__ = [
    "EMPTY",
    "ERROR",
    "METRICS_NAME",
    "ROWS",
    "TBD",
    "TaskMetrics",
    "compute",
    "contributed",
    "normalise_sql",
    "percentile",
    "read_task_metrics",
    "write_metrics",
]

#: Where 3.6 writes this. Committed, unlike the run directory it is computed from.
METRICS_NAME = "a1-trajectory-metrics.json"

#: This project's placeholder. A figure with no denominator says so rather than showing 0.
TBD = "TBD"

#: How the first ``execute_sql`` of a trajectory came back.
ERROR = "error"
EMPTY = "empty"
ROWS = "rows"

#: A bare identifier in a query, once literals and comments are gone.
_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

#: Tools whose ``table`` argument is checked against the final query's identifiers.
_TABLE_TOOLS = ("describe_table", "sample_rows")


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile: the value at ``ceil(fraction x n)``, one-indexed.

    **No interpolation, deliberately.** These are counts of tool calls and counts of turns —
    integers, and small ones. An interpolated p90 of 4.3 tool calls is a number no trajectory
    made, and a reader comparing it against `TURN_LIMIT` would be comparing against a value
    the loop cannot produce. Nearest rank always returns a value some trajectory actually had.
    """
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values")
    index = max(math.ceil(fraction * len(ordered)) - 1, 0)
    return ordered[index]


def _summary(values: Sequence[float]) -> dict[str, Any]:
    """Mean, median and p90 of a sample, or ``TBD`` when there is no sample.

    The count is beside them always. A p90 over three values is arithmetic rather than a
    statistic, and a reader has to be able to see which one they are looking at.
    """
    if not values:
        return {"n": 0, "mean": TBD, "median": TBD, "p90": TBD, "min": TBD, "max": TBD}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        # Mean of the two middle values at even n, which is `statistics.median`'s own rule.
        "median": statistics.median(values),
        "p90": percentile(values, 0.90),
        "min": min(values),
        "max": max(values),
    }


def _rate(numerator: int, denominator: int) -> Any:
    """A share, or ``TBD``. **A rate over nothing is not zero**, and writing 0.0 for one is
    how a denominator disappears from a table that quotes it.
    """
    return TBD if denominator == 0 else round(numerator / denominator, 6)


def normalise_sql(sql: str) -> str:
    """The form two statements are compared in to ask whether they are the same query.

    Whitespace collapsed, case folded, a trailing semicolon dropped. **Not an equivalence
    rule**: `equivalence.compare` decides whether two queries give the same answer by running
    them, and that is the only thing in this project allowed to say two queries agree. This
    only asks whether the model ran the text it later gave, which is a question about a
    trajectory rather than about SQL.
    """
    return " ".join(sql.split()).rstrip(";").strip().casefold()


def _identifiers(sql: str) -> set[str]:
    """Every bare word of a query, lowercased, with literals and comments blanked first."""
    return {word.casefold() for word in _WORD.findall(blank_literals(sql))}


def contributed(name: str, arguments: Mapping[str, Any], final_sql: str) -> bool:
    """Whether one tool call contributed to the final query. **See the module docstring** —
    this is the approximate half of the wasted-call rate and the whole of its definition.
    """
    if name == "execute_sql":
        sql = arguments.get("sql")
        return isinstance(sql, str) and normalise_sql(sql) == normalise_sql(final_sql)
    if name in _TABLE_TOOLS:
        table = arguments.get("table")
        return isinstance(table, str) and table.casefold() in _identifiers(final_sql)
    if name == "list_tables":
        # It made every table name knowable. The guard is against a final query that names
        # no table at all -- `SELECT 1` -- where nothing was discovered and nothing helped.
        return bool(_identifiers(final_sql) - {"select", "from", "where"})
    # An unknown tool name is a call the model made and got an error for. It cannot have
    # contributed to anything, and counting it as wasted is the honest reading.
    return False


@dataclass(frozen=True, slots=True)
class TaskMetrics:
    """One trajectory, reduced to what the four metrics need. **One task, one row.**"""

    task_id: str
    #: ``None`` when the ledger has no outcome for this task — a failed or unrun task. It is
    #: not ``False``: the model never got to be wrong, which is the same distinction
    #: `agents/results.py` keeps and for the same reason.
    solved: bool | None
    complete: bool
    termination: str | None
    turns: int
    tool_calls: int
    repair_attempts: int
    repair_succeeded: bool
    repair_blocked: str | None
    #: ``error``, ``empty``, ``rows``, or ``None`` when the trajectory ran no ``execute_sql``.
    first_execute_sql: str | None
    #: Whether **any** ``execute_sql`` of the trajectory errored, or returned no rows. The
    #: recovery rate keys on the *first* one, which is the roadmap's definition and is kept.
    #: These are reported beside it as counts because the smoke run showed the difference is
    #: real: 2 of one trajectory's 6 ``execute_sql`` calls returned no rows while its first
    #: returned one, so a run can have a thin first-call denominator and plenty of recovery
    #: happening inside it. A count is safe here; a fourth *rate* would be a fourth
    #: denominator, and 4.3 already puts two more beside these.
    any_execute_sql_error: bool
    any_execute_sql_empty: bool
    final_sql: str | None
    #: ``None`` when there was no final query or no tool calls — see the module docstring's
    #: two stated exclusions.
    wasted_calls: int | None = None
    #: Whether the ``end`` event's own counts agree with the events. A checksum, not a source.
    counts_agree: bool = True

    @property
    def eligible_for_waste(self) -> bool:
        return self.wasted_calls is not None


def _calls_by_id(events: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Every tool call's arguments, by ``call_id``.

    The arguments live once, in the assistant message that made the call — a ``tool_result``
    event carries the id and not the arguments, so that one copy cannot disagree with itself.
    This is the join that convention costs, and it is one pass.
    """
    calls: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if event["kind"] != MESSAGE:
            continue
        for call in event.get("tool_calls") or ():
            calls[call["id"]] = call
    return calls


def read_task_metrics(path: Path | str, *, solved: bool | None) -> TaskMetrics | None:
    """Reduce one transcript file. ``None`` when it holds no trajectory at all."""
    trajectories = read_trajectories(path)
    if not trajectories:
        return None
    # The last one stands. A task retried on resume appends a second bracket, and the earlier
    # one is evidence rather than a second measurement.
    trajectory = trajectories[-1]
    events = trajectory.events
    task_id = trajectory.task_id
    end = trajectory.end or {}

    assistants = [e for e in events if e["kind"] == MESSAGE and e["role"] == "assistant"]
    results = [e for e in events if e["kind"] == TOOL_RESULT]
    turns = len(assistants)
    tool_calls = len(results)

    queries = [e for e in results if e["name"] == "execute_sql"]
    first = queries[0] if queries else None
    if first is None:
        first_execute_sql = None
    elif not first["ok"]:
        first_execute_sql = ERROR
    elif (first.get("rows_returned") or 0) == 0:
        first_execute_sql = EMPTY
    else:
        first_execute_sql = ROWS

    # The final answer is the last thing the model said, read out of the transcript with the
    # same validator the agent used. Taking it from the ledger's `detail` instead would make
    # the wasted-call rate un-computable from the transcript alone, which it must be.
    final_sql = validate_answer(assistants[-1]["content"] if assistants else "").sql

    wasted: int | None = None
    if final_sql is not None and results:
        arguments = _calls_by_id(events)
        last_contributing = -1
        for index, result in enumerate(results):
            call = arguments.get(result["call_id"]) or {}
            if contributed(result["name"], call.get("arguments") or {}, final_sql):
                last_contributing = index
        wasted = len(results) - 1 - last_contributing

    return TaskMetrics(
        task_id=task_id,
        solved=solved,
        complete=trajectory.complete,
        termination=end.get("outcome"),
        turns=turns,
        tool_calls=tool_calls,
        repair_attempts=int(end.get("repair_attempts") or 0),
        repair_succeeded=bool(end.get("repair_succeeded")),
        repair_blocked=end.get("repair_blocked"),
        first_execute_sql=first_execute_sql,
        any_execute_sql_error=any(not e["ok"] for e in queries),
        any_execute_sql_empty=any(e["ok"] and (e.get("rows_returned") or 0) == 0 for e in queries),
        final_sql=final_sql,
        wasted_calls=wasted,
        counts_agree=(
            not trajectory.complete
            or (end.get("turns") == turns and end.get("tool_calls") == tool_calls)
        ),
    )


@dataclass(frozen=True, slots=True)
class Metrics:
    """The four, plus the denominators that are part of them."""

    run_id: str | None
    tasks: tuple[TaskMetrics, ...] = field(default_factory=tuple)

    def as_json(self) -> dict[str, Any]:
        return _project(self)


def _outcomes(ledger: Path) -> dict[str, bool | None]:
    """``task_id -> solved``, from the ledger. The last row for a task is the one that stands.

    ``None`` for a task the run did not complete. Absent rather than ``False`` is the rule
    `agents/results.py` follows: a task that failed never got to be wrong, and a ``False``
    here would put an infrastructure failure into a rate about the model.
    """
    outcomes: dict[str, bool | None] = {}
    for row in read_rows(ledger):
        if row.get("kind") != "task":
            continue
        task_id = str(row.get("task_id"))
        if row.get("status") != COMPLETE:
            outcomes[task_id] = None
        else:
            outcomes[task_id] = bool((row.get("detail") or {}).get("solved"))
    return outcomes


def compute(run_directory: Path | str) -> Metrics:
    """Read one run directory — its ledger and every transcript beside it."""
    directory = Path(run_directory)
    outcomes = _outcomes(directory / LEDGER_NAME)
    run_id = next(
        (str(row.get("run_id")) for row in read_rows(directory / LEDGER_NAME) if row.get("run_id")),
        None,
    )
    rows = []
    for path in sorted((directory / TRANSCRIPTS_DIR).glob("*.jsonl")):
        row = read_task_metrics(path, solved=outcomes.get(path.stem))
        if row is not None:
            rows.append(row)
    return Metrics(run_id=run_id, tasks=tuple(rows))


def _recovery(rows: Sequence[TaskMetrics], kinds: Sequence[str]) -> dict[str, Any]:
    eligible = [r for r in rows if r.first_execute_sql in kinds and r.solved is not None]
    recovered = [r for r in eligible if r.solved]
    return {
        "denominator": len(eligible),
        "recovered": len(recovered),
        "rate": _rate(len(recovered), len(eligible)),
    }


def _project(metrics: Metrics) -> dict[str, Any]:
    """Every figure, with its denominator and the sentence that defines it, in one mapping."""
    rows = metrics.tasks
    with_outcome = [r for r in rows if r.solved is not None]
    solved = [r for r in with_outcome if r.solved]
    waste_eligible = [r for r in rows if r.eligible_for_waste]
    wasted = sum(r.wasted_calls or 0 for r in waste_eligible)
    eligible_calls = sum(r.tool_calls for r in waste_eligible)

    return {
        "run_id": metrics.run_id,
        "computed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tasks": {
            "transcripts_read": len(rows),
            "with_outcome": len(with_outcome),
            "solved": len(solved),
            "incomplete_transcripts": sum(1 for r in rows if not r.complete),
            "counts_disagreeing_with_end": sum(1 for r in rows if not r.counts_agree),
            "terminations": dict(Counter(r.termination for r in rows if r.termination)),
        },
        "tool_calls_per_task": _summary([r.tool_calls for r in rows]),
        "turns_to_solve": {
            **_summary([r.turns for r in solved]),
            # String keys, because this file is written as JSON and read back by a test:
            # integer keys would survive `json.dumps` as strings and stop matching.
            "distribution": {
                str(turns): count
                for turns, count in sorted(Counter(r.turns for r in solved).items())
            },
        },
        "recovery_rate": {
            "error": _recovery(rows, [ERROR]),
            "empty": _recovery(rows, [EMPTY]),
            "error_or_empty": _recovery(rows, [ERROR, EMPTY]),
            "first_execute_sql": dict(Counter(r.first_execute_sql or "none" for r in rows)),
            "tasks_with_no_execute_sql": sum(1 for r in rows if r.first_execute_sql is None),
            # Counts, not rates. See `TaskMetrics.any_execute_sql_error`.
            "trajectories_with_any_execute_sql_error": sum(
                1 for r in rows if r.any_execute_sql_error
            ),
            "trajectories_with_any_execute_sql_empty": sum(
                1 for r in rows if r.any_execute_sql_empty
            ),
        },
        "wasted_calls": {
            "tasks_counted": len(waste_eligible),
            "tasks_without_final_sql": sum(1 for r in rows if r.final_sql is None),
            "tasks_without_tool_calls": sum(1 for r in rows if r.tool_calls == 0),
            "calls_counted": eligible_calls,
            "wasted": wasted,
            "rate": _rate(wasted, eligible_calls),
            "per_task_mean": (
                round(statistics.fmean([r.wasted_calls or 0 for r in waste_eligible]), 4)
                if waste_eligible
                else TBD
            ),
        },
        "repairs": {
            "attempts": sum(r.repair_attempts for r in rows),
            "successes": sum(1 for r in rows if r.repair_succeeded),
            "blocked": dict(Counter(r.repair_blocked for r in rows if r.repair_blocked)),
        },
        "definitions": {
            "tool_calls_per_task": (
                "tool_result events, not the tool calls an assistant message asked for: a "
                "trajectory stopped at the turn limit keeps calls that were never executed."
            ),
            "turns_to_solve": (
                "assistant messages, over solved tasks only. A repair turn counts, because "
                "it is a request the run paid for."
            ),
            "recovery_rate": (
                "three denominators, never one. `error` is the headline: the first "
                "execute_sql came back ok=false. `empty` is reported apart because an empty "
                "result can be the correct answer -- docs/EQUIVALENCE.md prices that at "
                "4.7389% of the frame -- so that denominator holds tasks with nothing to "
                "recover from. A task with no execute_sql is in none of them."
            ),
            "wasted_calls": (
                "calls after the last one that contributed to the final query. Contributed: "
                "an execute_sql whose sql equals the final answer normalised; a "
                "describe_table or sample_rows whose table appears in the final query's "
                "identifiers; a list_tables whenever the final query names a table. "
                "APPROXIMATE, and in one direction: a describe_table on a table the final "
                "query never mentions is counted wasted even when it was the call that "
                "ruled that table out. An upper bound on waste, not a measurement of it."
            ),
            "percentile": "nearest rank, ceil(fraction x n), no interpolation.",
            "median": "mean of the two middle values at even n.",
        },
    }


def write_metrics(run_directory: Path | str, destination: Path | str) -> dict[str, Any]:
    """Compute and write. The one place 3.6's committed metrics file is produced."""
    payload = compute(run_directory).as_json()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload
