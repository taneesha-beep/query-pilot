"""5.1 — the escalation rule: which cheap trajectories are handed to the strong model.

**A2 is a cascade.** A task is run first by A1's loop on the cheap model; this rule reads the
trajectory that came back and says whether the task goes to the strong model. **One escalation
at most**, and a task that escalates has paid for both attempts: the cheap trajectory's tokens
count in its cost whatever the strong model then does.

**The four clauses.** Escalate when any of these holds of the cheap trajectory:

1. ``did_not_answer`` — it terminated on ``turn_limit``, ``tool_call_limit``, ``prompt_ceiling``
   or ``provider_rejected`` rather than ``answer``. Named on its own because constraint 61 gives
   those trajectories no repair, which left "failed validation after its repair" ambiguous for
   exactly the trajectories most worth escalating. **``provider_rejected`` was added on
   2026-09-12, before this rule was committed**, by the author's decision after the cheap-model
   preflight: Groq refused the cheap model's own output on 6 of 19 of its trajectories, which
   under the old retry rule were failed tasks this rule never saw. A run that declares
   ``rejected_generation = "score_unsolved"`` ends those trajectories complete and unsolved,
   and they escalate here. 3.6's run has none, so its table is unchanged.
2. ``validation_failed`` — its final reply failed validation, after its one repair where one was
   made. Read as **the final validation failed, whatever the termination**: that is the reading
   5.1's table was computed with, and on a trajectory that did not answer it coincides with
   clause 1 whenever the last text held no statement. On 3.6's run it fired on the same eight
   trajectories clause 1 did.
3. ``first_query_error`` — its first ``execute_sql`` came back ``ok=false``.
4. ``first_query_empty`` — its first ``execute_sql`` came back with zero rows.

**Not a clause, and deliberately:** a trajectory that ran no ``execute_sql`` at all. 3.6 measured
those as solving above the average, so escalating them would spend the strong model on tasks the
cheap one had already got right. It is counted beside the clauses so the rejection stays visible.

**Where each input comes from.** Clauses 1 and 2 read the ledger task row's ``detail`` —
``termination`` and ``validation_rule`` — which is the outcome record (constraint 51). Clauses 3
and 4 read the transcript through 3.4's own reader, :func:`~query_pilot.agents.metrics.
read_task_metrics`, over the last bracket (constraint 86). The transcript's ``end`` outcome is
checked against the ledger's ``termination``, and a disagreement raises rather than being settled
in favour of either record.

**Only a complete trajectory is decided.** ``budget``, or a trajectory that raised, is a failed
task, which the run retries rather than escalates; asking this rule about one is refused, and a
run's failed or unrun tasks are listed apart rather than decided.

**Frozen.** Committed before any 5.2 run. Tuning a clause after seeing which way the cascade
lands is the specific failure 5.1 exists to prevent (constraint 78), and :data:`DEFINITIONS`
travels into every file this module's output is written to, the way 3.4's definitions do.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from query_pilot.agents.a1 import (
    ANSWER,
    PROMPT_CEILING,
    PROVIDER_REJECTED,
    TOOL_CALL_LIMIT_REACHED,
    TURN_LIMIT_REACHED,
)
from query_pilot.agents.attempts import loop_behaviour, read_attempt_sizes, summarise_sizes
from query_pilot.agents.metrics import EMPTY, ERROR, ROWS, TBD, read_task_metrics
from query_pilot.agents.transcript import transcript_path
from query_pilot.agents.validate import RULES
from query_pilot.run.ledger import COMPLETE, LEDGER_NAME, read_rows

__all__ = [
    "CLAUSES",
    "COMPLETING",
    "DEFINITIONS",
    "DID_NOT_ANSWER",
    "FIRST_QUERY_EMPTY",
    "FIRST_QUERY_ERROR",
    "NO_EXECUTE_SQL",
    "VALIDATION_FAILED",
    "Decisions",
    "Escalation",
    "NotDecidable",
    "TaskDecision",
    "decide",
    "document",
    "read_decisions",
    "tabulate",
]

DID_NOT_ANSWER: Final = "did_not_answer"
VALIDATION_FAILED: Final = "validation_failed"
FIRST_QUERY_ERROR: Final = "first_query_error"
FIRST_QUERY_EMPTY: Final = "first_query_empty"

#: In the roadmap's order. An escalation lists every clause that fired, in this order.
CLAUSES: Final = (DID_NOT_ANSWER, VALIDATION_FAILED, FIRST_QUERY_ERROR, FIRST_QUERY_EMPTY)

#: The rejected candidate, counted beside the clauses and never one of them.
NO_EXECUTE_SQL: Final = "no_execute_sql"

#: The five ways a trajectory ends as a **complete** task. ``budget`` is not one: it fails it.
COMPLETING: Final = (
    ANSWER,
    TURN_LIMIT_REACHED,
    TOOL_CALL_LIMIT_REACHED,
    PROMPT_CEILING,
    PROVIDER_REJECTED,
)

#: How 3.4's reader reports a trajectory's first ``execute_sql``; ``None`` when it ran none.
_FIRST_EXECUTE_SQL: Final = (ERROR, EMPTY, ROWS, None)

DEFINITIONS: Final = {
    "rule": (
        "Escalate a cheap trajectory to the strong model when any of the four clauses fires. "
        "One escalation at most. The cheap trajectory's tokens count in the escalated task's "
        "cost, and an escalated task the strong model also fails has paid for both."
    ),
    DID_NOT_ANSWER: (
        "The trajectory terminated on turn_limit, tool_call_limit, prompt_ceiling or "
        "provider_rejected rather than answer. Read from the ledger task row's "
        "detail.termination. provider_rejected was added on 2026-09-12, before the rule was "
        "committed, by the author's decision after the cheap-model preflight; it exists only in "
        "runs that declare rejected_generation = score_unsolved."
    ),
    VALIDATION_FAILED: (
        "The final reply failed validation, after its one repair where one was made: "
        "detail.validation_rule is not null, whatever the termination. A trajectory that did "
        "not answer gets no repair (constraint 61), so on those this clause coincides with "
        "did_not_answer whenever the last text held no statement."
    ),
    FIRST_QUERY_ERROR: (
        "The trajectory's first execute_sql came back ok=false. Read from the transcript's last "
        "bracket by metrics.read_task_metrics."
    ),
    FIRST_QUERY_EMPTY: (
        "The trajectory's first execute_sql came back ok with zero rows. Read the same way."
    ),
    NO_EXECUTE_SQL: (
        "NOT a clause. A trajectory that ran no execute_sql at all is not escalated: on 3.6's "
        "run such trajectories solved above the average, so it is not a failure signal. "
        "Counted beside the clauses so the rejection stays visible."
    ),
    "decidable": (
        "Complete trajectories only. A failed task (budget, a client error) is retried by the "
        "run, never escalated, and is listed as undecided."
    ),
    "frozen": (
        "Committed before any 5.2 run. Tuning a clause after seeing 5.2's runs is the failure "
        "5.1 exists to prevent (constraint 78)."
    ),
}


class NotDecidable(ValueError):
    """The rule was asked about something it does not decide. Refused rather than guessed."""


@dataclass(frozen=True, slots=True)
class Escalation:
    """Which clauses fired, in :data:`CLAUSES` order. Empty means the cheap answer stands."""

    clauses: tuple[str, ...] = ()

    @property
    def escalate(self) -> bool:
        return bool(self.clauses)


def decide(
    *, termination: str, validation_rule: str | None, first_execute_sql: str | None
) -> Escalation:
    """The rule, on its three inputs. **Every clause that fires is reported, not the first.**

    Each input is checked against the values its writer can produce, so a renamed slug or a
    failed task fails loudly here instead of reading as "nothing fired".
    """
    if termination not in COMPLETING:
        raise NotDecidable(
            f"termination {termination!r} is not a complete trajectory; the rule decides "
            f"{', '.join(COMPLETING)} only. A failed task is retried by the run, not escalated."
        )
    if validation_rule is not None and validation_rule not in RULES:
        raise NotDecidable(
            f"validation_rule {validation_rule!r} is not one of validate.RULES {RULES}"
        )
    if first_execute_sql not in _FIRST_EXECUTE_SQL:
        raise NotDecidable(
            f"first_execute_sql {first_execute_sql!r} is not one of {_FIRST_EXECUTE_SQL}"
        )
    fired = {
        DID_NOT_ANSWER: termination != ANSWER,
        VALIDATION_FAILED: validation_rule is not None,
        FIRST_QUERY_ERROR: first_execute_sql == ERROR,
        FIRST_QUERY_EMPTY: first_execute_sql == EMPTY,
    }
    return Escalation(tuple(clause for clause in CLAUSES if fired[clause]))


@dataclass(frozen=True, slots=True)
class TaskDecision:
    """One complete trajectory: the rule's three inputs, what it decided, and the outcome."""

    task_id: str
    termination: str
    validation_rule: str | None
    first_execute_sql: str | None
    solved: bool
    escalation: Escalation

    def as_json(self, *, outcomes: bool) -> dict[str, Any]:
        row: dict[str, Any] = {
            "task_id": self.task_id,
            "termination": self.termination,
            "validation_rule": self.validation_rule,
            "first_execute_sql": self.first_execute_sql,
            "clauses": list(self.escalation.clauses),
        }
        if outcomes:
            row["solved"] = self.solved
        return row


@dataclass(frozen=True, slots=True)
class Decisions:
    """Every task a run declared: decided if it completed, listed as undecided if not."""

    run_id: str | None
    decided: tuple[TaskDecision, ...]
    #: Declared tasks with no complete row — failed, or never run. Never decided.
    undecided: tuple[str, ...]


def read_decisions(run_directory: Path | str) -> Decisions:
    """Apply the rule to every complete task of one run directory. Spends nothing.

    **The last ledger row for a task stands**, as everywhere else: a task retried on resume has
    a ``failed`` row and then a ``complete`` one, and only the second is its outcome.
    """
    directory = Path(run_directory)
    run_id: str | None = None
    declared: list[str] = []
    last: dict[str, Mapping[str, Any]] = {}
    for row in read_rows(directory / LEDGER_NAME):
        run_id = run_id or row.get("run_id")
        kind = row.get("kind")
        if kind == "run_start":
            for task_id in (row.get("declared") or {}).get("task_ids") or ():
                if task_id not in declared:
                    declared.append(task_id)
        elif kind == "task":
            task_id = str(row["task_id"])
            last[task_id] = row
            if task_id not in declared:
                declared.append(task_id)

    decided: list[TaskDecision] = []
    undecided: list[str] = []
    for task_id in declared:
        row = last.get(task_id)
        if row is None or row.get("status") != COMPLETE:
            undecided.append(task_id)
            continue
        detail = row.get("detail") or {}
        solved = bool(detail.get("solved"))
        metrics = read_task_metrics(transcript_path(directory, task_id), solved=solved)
        if metrics is None or not metrics.complete:
            raise ValueError(
                f"{task_id}: the ledger records a complete task and its transcript holds no "
                f"closed trajectory"
            )
        termination = detail.get("termination")
        if metrics.termination != termination:
            raise ValueError(
                f"{task_id}: the transcript ended on {metrics.termination!r} and the ledger "
                f"records {termination!r}; the two records disagree"
            )
        escalation = decide(
            termination=str(termination),
            validation_rule=detail.get("validation_rule"),
            first_execute_sql=metrics.first_execute_sql,
        )
        decided.append(
            TaskDecision(
                task_id=task_id,
                termination=str(termination),
                validation_rule=detail.get("validation_rule"),
                first_execute_sql=metrics.first_execute_sql,
                solved=solved,
                escalation=escalation,
            )
        )
    return Decisions(run_id=run_id, decided=tuple(decided), undecided=tuple(undecided))


def _rate(numerator: int, denominator: int) -> Any:
    """A share, or ``TBD`` over nothing — `metrics._rate`'s rule, for the same reason."""
    return TBD if denominator == 0 else round(numerator / denominator, 6)


def tabulate(decided: Sequence[TaskDecision], *, outcomes: bool) -> dict[str, Any]:
    """5.1's table: how often the rule and each clause fire, and — with outcomes — what solved.

    ``outcomes=False`` is the preflight's form. 5.1 asks the preflight for how often each
    clause fires, and a per-clause solve rate over fifteen cheap trajectories is exactly the
    number a person would tune a clause against, so it is not computed at all.
    """

    def cell(rows: Sequence[TaskDecision]) -> dict[str, Any]:
        out: dict[str, Any] = {"count": len(rows)}
        if outcomes:
            solved = sum(1 for row in rows if row.solved)
            out["solved"] = solved
            out["solve_rate"] = _rate(solved, len(rows))
        return out

    escalated = [row for row in decided if row.escalation.escalate]
    table: dict[str, Any] = {
        "trajectories": len(decided),
        "would_escalate": cell(escalated),
        "would_not_escalate": cell([row for row in decided if not row.escalation.escalate]),
        "escalation_rate": _rate(len(escalated), len(decided)),
        "clauses": {
            clause: cell([row for row in decided if clause in row.escalation.clauses])
            for clause in CLAUSES
        },
        "fired_alone": {
            clause: sum(1 for row in decided if row.escalation.clauses == (clause,))
            for clause in CLAUSES
        },
        "not_a_clause": {
            NO_EXECUTE_SQL: cell([row for row in decided if row.first_execute_sql is None])
        },
    }
    if outcomes:
        # Recall: of the trajectories that did not solve, how many the rule would send on. The
        # cascade can only recover a failure that announces itself, and this is how many do.
        failures = [row for row in decided if not row.solved]
        caught = [row for row in failures if row.escalation.escalate]
        table["failures"] = {
            "count": len(failures),
            "escalated": len(caught),
            "recall": _rate(len(caught), len(failures)),
        }
    return table


def _requests(ledger: Path) -> dict[str, Any]:
    """Who served the run and when, from **every** attempt row — refused ones included.

    Every row, because this is where a request served by a model other than the one declared
    would show: a spillover is an attempt row naming a different provider or model.
    """
    by_endpoint: Counter[str] = Counter()
    by_outcome: Counter[str] = Counter()
    by_pool: Counter[str] = Counter()
    stamps: list[str] = []
    failed_rows: Counter[str] = Counter()
    failed_tasks: Counter[str] = Counter()
    for row in read_rows(ledger):
        if row.get("kind") == "attempt":
            by_endpoint[f"{row.get('provider')} {row.get('model')}"] += 1
            by_outcome[str(row.get("outcome"))] += 1
            by_pool[str(row.get("pool"))] += 1
            if row.get("recorded_at"):
                stamps.append(str(row["recorded_at"]))
        elif row.get("kind") == "task" and row.get("status") != COMPLETE:
            failed_rows[str(row.get("error_class"))] += 1
            failed_tasks[str(row.get("task_id"))] += 1
    stamps.sort()
    return {
        "attempt_rows": sum(by_outcome.values()),
        "by_outcome": dict(sorted(by_outcome.items())),
        "by_endpoint": dict(sorted(by_endpoint.items())),
        "by_pool": dict(sorted(by_pool.items())),
        "first_recorded_at": stamps[0] if stamps else None,
        "last_recorded_at": stamps[-1] if stamps else None,
        "failed_task_rows": dict(sorted(failed_rows.items())),
        "tasks_failed_more_than_once": sorted(t for t, n in failed_tasks.items() if n > 1),
    }


def document(
    run_directory: Path | str,
    *,
    outcomes: bool,
    tpm: int,
    ledger: str,
    ceiling_derivation: Mapping[str, Any],
) -> dict[str, Any]:
    """Everything one committed escalation file holds, from one run directory. Spends nothing.

    No timestamp of its own, so that regenerating it from the same directory reproduces it
    byte for byte — which is what lets a test compare the whole committed file rather than a
    chosen part of it. ``ledger`` names the run's own ledger file (constraint 12), whatever
    directory the rows were read from.
    """
    directory = Path(run_directory)
    decisions = read_decisions(directory)
    sizes = read_attempt_sizes(directory)
    return {
        "run_id": decisions.run_id,
        "ledger": ledger,
        "requests": _requests(directory / LEDGER_NAME),
        "definitions": DEFINITIONS,
        "outcomes_included": outcomes,
        "tasks_declared": len(decisions.decided) + len(decisions.undecided),
        "undecided": list(decisions.undecided),
        "terminations": dict(sorted(Counter(r.termination for r in decisions.decided).items())),
        "table": tabulate(decisions.decided, outcomes=outcomes),
        "attempt_sizes": summarise_sizes(sizes, tpm=tpm),
        "ceiling_derivation": dict(ceiling_derivation),
        "loop_behaviour": loop_behaviour(directory),
        "tasks": [row.as_json(outcomes=outcomes) for row in decisions.decided],
    }
