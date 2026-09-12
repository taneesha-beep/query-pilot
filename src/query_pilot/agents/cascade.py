"""A2, 5.2's cascade — composed from two committed runs and 5.1's frozen rule. **Spends nothing.**

A2 runs A1's loop on the cheap model and hands a task to the strong model when the escalation
rule (`agents/escalation.py`, frozen at ``c1f8520``) fires on the cheap trajectory. **It is
composed, not run** — decided with the author before any figure existed (the roadmap's session 13
entry). Each task takes the always-cheap run's outcome when the rule is silent, and 3.6's outcome
on the same task when it fires. An escalation restarts A1 on the strong model from nothing — the
agent, prompt, limits and model 3.6 ran — so 3.6's trajectory is a draw of exactly that; what
composing gives up is a draw taken on the same day under the same rejection policy. **The
comparison is paired**: on an escalated task the cascade and always-strong share one strong
trajectory, so every difference between them comes from the tasks the rule left with the cheap
model.

**Cost is recorded tokens** — prompt plus completion of every attempt row, attributed to its task
by the row's ``task_id``, so a retried task's earlier attempts are counted where they were spent.
That is the basis 3.6's committed 6,646.8 a solved task already stands on, and it is exact for
the cascade too: the escalated tasks' strong cost is 3.6's own rows for those tasks. A request the
provider refused has no attempt row and no recorded tokens (constraint 92); those are counted and
declared, never estimated. The per-task basis — each task's standing trajectory alone — is
written beside it, labelled.

The run ledgers are gitignored, so what CI cannot re-read is carried in the output: each run's
tokens and attempt rows spent **before** its standing trajectory, per task, and its refused
requests, per task. :func:`compose` refuses a carried map that does not reconcile exactly with
the committed projection's own totals, which is what lets a test regenerate the whole file from
committed inputs.

**No verdict threshold was chosen for "near".** There is no second draw of either model, so there
is no measured noise band to derive one from. The verdict is 5.2's metric alone, fixed in writing
before the cheap run finished: the cascade wins iff its cost per solved task is strictly below
always-strong's. Everything else here is reported beside it, not as a verdict.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from query_pilot.agents.a1 import PROVIDER_REJECTED
from query_pilot.agents.escalation import CLAUSES, decide
from query_pilot.agents.escalation import DEFINITIONS as RULE
from query_pilot.run.ledger import COMPLETE, read_rows

__all__ = [
    "AGENT",
    "DEFINITIONS",
    "NotComposable",
    "Side",
    "attempts_by_task",
    "compose",
    "earlier_attempts",
    "refused_requests",
]

#: The cascade's agent string. `results_name` turns it into ``a2-working.json``.
AGENT = "A2"

#: The cheap and strong agents a cascade is composed from.
CHEAP_AGENT = "A2-cheap"
STRONG_AGENT = "A1"

TBD = "TBD"

#: Written into the output, frozen with it — the rule 59 applies to 3.4's metrics.
DEFINITIONS: dict[str, str] = {
    "composition": (
        "Composed, not run. A task whose cheap trajectory the frozen escalation rule is silent "
        "on takes the always-cheap run's outcome; a task it fires on takes 3.6's outcome on the "
        "same task (the trajectory that stands). One escalation at most. The escalated task's "
        "cheap tokens count as well as its strong ones."
    ),
    "cost": (
        "Recorded tokens: prompt plus completion of every attempt row in the run's ledger, "
        "attributed to its task by the row's task_id, retried attempts included. Always-cheap "
        "and always-strong: every row of their run. Cascade: every cheap row, all tasks, plus "
        "3.6's rows for the escalated tasks. Cost per solved task is that over solved tasks."
    ),
    "cost_standing_trajectories_only": (
        "Beside the headline and labelled: each task's standing trajectory alone (the per-task "
        "prompt and completion tokens in the committed projection), leaving out attempts "
        "spent before a retry."
    ),
    "refused_requests": (
        "Requests the provider answered with HTTP 400: a provider_rejected termination, a repair "
        "whose request was refused (repair_blocked = provider_rejected), and a task row that "
        "failed as bad_request. None has an attempt row and no ledger records its tokens, so "
        "every cost here is short by them. Counted, never estimated."
    ),
    "verdict": (
        "Fixed before the always-cheap run was complete: the cascade wins 5.2's metric iff its "
        "cost per solved task (recorded tokens) is strictly below always-strong's; otherwise it "
        "loses. Compared exactly, as cross-multiplied integers, never on rounded figures."
    ),
    "near": (
        "No threshold. No second draw of either model exists, so no measured noise band to "
        "derive one from; the difference from always-strong is written in tasks and as a token "
        "ratio, without an adjective."
    ),
    "frontier": (
        "On (solved tasks, recorded tokens): an agent is dominated by another that solved at "
        "least as many tasks for at most as many tokens, and is strictly better on one."
    ),
    "break_even_price_ratio": (
        "Not a verdict. Every token here is one unit and costs $0.00, whichever model spent it. "
        "r* = (S * N_cascade / N_strong - S_escalated) / C, where C is the cascade's cheap "
        "tokens, S_escalated its strong tokens, and S and N_strong always-strong's tokens and "
        "solved tasks: at any price of a cheap token relative to a strong one below r*, the "
        "cascade's cost per solved task is below always-strong's. r* <= 0: no price does it."
    ),
}


class NotComposable(ValueError):
    """The inputs do not describe two complete runs of the same tasks and one rule. Refused."""


@dataclass(frozen=True, slots=True)
class Side:
    """One committed run, as the cascade reads it.

    ``earlier`` maps a task to the attempt rows and tokens its ledger records **before** the
    trajectory that stands — non-zero only for a retried task. ``refused`` maps a task to the
    requests the provider refused on it. Both come from the gitignored ledger through
    :func:`earlier_attempts` and :func:`refused_requests`, and are carried in the output.
    """

    projection: Mapping[str, Any]
    earlier: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    refused: Mapping[str, int] = field(default_factory=dict)


# --- what the ledgers hold that the projections do not -----------------------------------------


def attempts_by_task(ledger: Path | str) -> dict[str, dict[str, int]]:
    """Every attempt row of one ledger, summed per task: rows and recorded tokens."""
    out: dict[str, dict[str, int]] = {}
    for row in read_rows(ledger):
        if row.get("kind") != "attempt":
            continue
        task_id = row.get("task_id")
        if not task_id:
            raise NotComposable(f"{ledger}: an attempt row names no task; it cannot be attributed")
        bucket = out.setdefault(str(task_id), {"attempt_rows": 0, "tokens": 0})
        bucket["attempt_rows"] += 1
        bucket["tokens"] += (row.get("prompt_tokens") or 0) + (row.get("completion_tokens") or 0)
    return out


def _standing(task: Mapping[str, Any]) -> tuple[int, int]:
    """Attempt rows and tokens of a task's standing trajectory, from its projected row."""
    tokens = (task.get("prompt_tokens") or 0) + (task.get("completion_tokens") or 0)
    return int(task.get("turns") or 0), int(tokens)


def earlier_attempts(
    projection: Mapping[str, Any], attempts: Mapping[str, Mapping[str, int]]
) -> dict[str, dict[str, int]]:
    """What each task spent before the trajectory that stands: the ledger's rows less its own.

    A task's standing trajectory makes one attempt row per turn (a repair is a turn, a refused
    request is not), so the remainder is the attempts of the brackets a retry replaced. A
    negative remainder means the two records disagree, and raises.
    """
    out: dict[str, dict[str, int]] = {}
    tasks = {str(task["task_id"]): task for task in projection["tasks"]}
    for task_id in sorted(set(tasks) | set(attempts)):
        if task_id not in tasks:
            raise NotComposable(f"{task_id}: attempt rows for a task the projection does not hold")
        rows, tokens = _standing(tasks[task_id])
        spent = attempts.get(task_id, {"attempt_rows": 0, "tokens": 0})
        extra = {
            "attempt_rows": spent["attempt_rows"] - rows,
            "tokens": spent["tokens"] - tokens,
        }
        if extra["attempt_rows"] < 0 or extra["tokens"] < 0:
            raise NotComposable(
                f"{task_id}: the standing trajectory records more than the ledger holds ({extra})"
            )
        if extra["attempt_rows"] or extra["tokens"]:
            out[task_id] = extra
    return out


def refused_requests(ledger: Path | str) -> dict[str, int]:
    """Requests the provider refused with HTTP 400, per task, from every task row of a ledger."""
    refused: Counter[str] = Counter()
    for row in read_rows(ledger):
        if row.get("kind") != "task":
            continue
        task_id = str(row.get("task_id"))
        detail = row.get("detail") or {}
        if row.get("status") == COMPLETE:
            refused[task_id] += detail.get("termination") == PROVIDER_REJECTED
            refused[task_id] += detail.get("repair_blocked") == PROVIDER_REJECTED
        else:
            refused[task_id] += row.get("error_class") == "bad_request"
    return {task_id: n for task_id, n in sorted(refused.items()) if n}


# --- composing ----------------------------------------------------------------------------------


def _check_complete(name: str, projection: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    run = projection.get("run") or {}
    accuracy = projection.get("execution_accuracy") or {}
    declared = (projection.get("measurement") or {}).get("tasks_declared")
    tasks = {str(task["task_id"]): task for task in projection.get("tasks") or ()}
    if (
        not declared
        or run.get("tasks_failed") != 0
        or run.get("tasks_complete") != declared
        or len(tasks) != declared
        or not isinstance(accuracy.get("percent"), (int, float))
    ):
        raise NotComposable(
            f"{name}: not a complete run ({run.get('tasks_complete')} of {declared} complete, "
            f"{run.get('tasks_failed')} failed, accuracy {accuracy.get('percent')!r}); a cascade "
            f"is composed from two complete runs only"
        )
    return tasks


def _reconcile(name: str, side: Side, tasks: Mapping[str, Mapping[str, Any]]) -> None:
    """The carried maps must account for the projection's totals exactly, or they are wrong."""
    for task_id in (*side.earlier, *side.refused):
        if task_id not in tasks:
            raise NotComposable(f"{name}: {task_id} is carried but is not one of the run's tasks")
    rows = sum(_standing(task)[0] for task in tasks.values())
    tokens = sum(_standing(task)[1] for task in tasks.values())
    rows += sum(extra["attempt_rows"] for extra in side.earlier.values())
    tokens += sum(extra["tokens"] for extra in side.earlier.values())
    run_rows = side.projection["run"]["attempts"]
    run_tokens = side.projection["tokens"]["total"]
    if (rows, tokens) != (run_rows, run_tokens):
        raise NotComposable(
            f"{name}: standing trajectories plus earlier attempts come to {rows} rows and "
            f"{tokens} tokens; the run recorded {run_rows} and {run_tokens}"
        )
    for task_id, task in tasks.items():
        if task.get("termination") == PROVIDER_REJECTED and not side.refused.get(task_id):
            raise NotComposable(
                f"{name}: {task_id} ended provider_rejected and no refusal is carried"
            )


def _check_rule(
    escalation: Mapping[str, Any], cheap: Mapping[str, Mapping[str, Any]]
) -> dict[str, tuple[str, ...]]:
    """The committed decisions, re-decided: the frozen rule, every task, agreeing inputs."""
    if escalation.get("definitions") != RULE:
        raise NotComposable("the escalation file's definitions are not the frozen rule's")
    if escalation.get("undecided"):
        raise NotComposable(f"undecided tasks: {escalation['undecided']}")
    clauses: dict[str, tuple[str, ...]] = {}
    for row in escalation["tasks"]:
        task_id = row["task_id"]
        task = cheap.get(task_id)
        if task is None:
            raise NotComposable(f"{task_id}: decided, but not a task of the cheap run")
        if (row["termination"], row["validation_rule"], row.get("solved")) != (
            task.get("termination"),
            task.get("validation_rule"),
            task.get("solved"),
        ):
            raise NotComposable(f"{task_id}: the decision's inputs disagree with the cheap run")
        decided = decide(
            termination=row["termination"],
            validation_rule=row["validation_rule"],
            first_execute_sql=row["first_execute_sql"],
        ).clauses
        if list(decided) != list(row["clauses"]):
            raise NotComposable(f"{task_id}: the file says {row['clauses']}, the rule {decided}")
        clauses[task_id] = decided
    if set(clauses) != set(cheap):
        raise NotComposable("the escalation file does not decide every task of the cheap run")
    return clauses


def _model(projection: Mapping[str, Any]) -> str:
    models = (projection.get("measurement") or {}).get("provider_and_model") or []
    if len(models) != 1:
        raise NotComposable(f"a side must be served by one model, not {models}")
    return str(models[0])


def _per_solved(tokens: int, solved: int) -> Any:
    return round(tokens / solved, 1) if solved else TBD


def _percent(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 4)


def _source(side: Side, results: str) -> dict[str, Any]:
    measurement = side.projection["measurement"]
    return {
        "agent": measurement["agent"],
        "provider_and_model": measurement["provider_and_model"],
        "date": measurement["date"],
        "started_at": measurement["started_at"],
        "ended_at": measurement["ended_at"],
        "run_id": measurement["run_id"],
        "ledger": measurement["ledger"],
        "results": results,
        "earlier_attempts": {k: dict(v) for k, v in sorted(side.earlier.items())},
        "refused_requests": dict(sorted(side.refused.items())),
    }


def _pairs(first: Iterable[bool], second: Iterable[bool], names: tuple[str, str]) -> dict:
    counted = Counter(zip(first, second, strict=True))
    return {
        "both": counted[(True, True)],
        f"{names[0]}_only": counted[(True, False)],
        f"{names[1]}_only": counted[(False, True)],
        "neither": counted[(False, False)],
    }


def compose(
    cheap: Side,
    strong: Side,
    escalation: Mapping[str, Any],
    *,
    sources: Mapping[str, str],
    empty_reference: Sequence[str],
    wrong_reference: Sequence[str],
) -> dict[str, Any]:
    """The cascade's committed document, from committed inputs and the two carried maps.

    ``sources`` names the three committed files read (``cheap``, ``strong``, ``escalation``).
    ``empty_reference`` is the split's tasks whose reference returns no rows, and
    ``wrong_reference`` the tasks 4.4 verified return wrong data: the rows constraints 70 and 84
    say an accuracy figure can move on. No timestamp, so that regenerating it reproduces it.
    """
    cheap_tasks = _check_complete(CHEAP_AGENT, cheap.projection)
    strong_tasks = _check_complete(STRONG_AGENT, strong.projection)
    if cheap.projection["measurement"]["agent"] != CHEAP_AGENT:
        raise NotComposable(f"the cheap side must be {CHEAP_AGENT}")
    if strong.projection["measurement"]["agent"] != STRONG_AGENT:
        raise NotComposable(f"the strong side must be {STRONG_AGENT}")
    if list(cheap_tasks) != list(strong_tasks):
        raise NotComposable("the two runs did not declare the same tasks in the same order")
    _reconcile(CHEAP_AGENT, cheap, cheap_tasks)
    _reconcile(STRONG_AGENT, strong, strong_tasks)
    clauses = _check_rule(escalation, cheap_tasks)
    for task_id in (*empty_reference, *wrong_reference):
        if task_id not in cheap_tasks:
            raise NotComposable(f"{task_id} is named as a reference row but is not a task here")
    cheap_model, strong_model = _model(cheap.projection), _model(strong.projection)

    def spent(side: Side, task: Mapping[str, Any]) -> tuple[int, int]:
        rows, tokens = _standing(task)
        extra = side.earlier.get(str(task["task_id"]), {})
        return rows + extra.get("attempt_rows", 0), tokens + extra.get("tokens", 0)

    tasks: list[dict[str, Any]] = []
    for task_id, cheap_task in cheap_tasks.items():
        strong_task = strong_tasks[task_id]
        escalated = bool(clauses[task_id])
        cheap_rows, cheap_tokens = spent(cheap, cheap_task)
        strong_rows, strong_tokens = spent(strong, strong_task) if escalated else (0, 0)
        tasks.append(
            {
                "task_id": task_id,
                "db_id": cheap_task.get("db_id"),
                "clauses": list(clauses[task_id]),
                "escalated": escalated,
                "solved": bool((strong_task if escalated else cheap_task).get("solved")),
                "cheap_solved": bool(cheap_task.get("solved")),
                "strong_solved": bool(strong_task.get("solved")),
                "attempt_rows": {"cheap": cheap_rows, "strong": strong_rows},
                "tokens": {"cheap": cheap_tokens, "strong": strong_tokens},
                "tokens_standing": {
                    "cheap": _standing(cheap_task)[1],
                    "strong": _standing(strong_task)[1] if escalated else 0,
                },
                "refused_requests": {
                    "cheap": cheap.refused.get(task_id, 0),
                    "strong": strong.refused.get(task_id, 0) if escalated else 0,
                },
            }
        )

    declared = len(tasks)
    escalated = [task for task in tasks if task["escalated"]]
    silent = [task for task in tasks if not task["escalated"]]
    solved = sum(task["solved"] for task in tasks)
    c_tokens = sum(task["tokens"]["cheap"] for task in tasks)
    s_tokens = sum(task["tokens"]["strong"] for task in tasks)
    c_standing = sum(task["tokens_standing"]["cheap"] for task in tasks)
    s_standing = sum(task["tokens_standing"]["strong"] for task in tasks)

    def point(agent: str, n: int, total: int, standing: int, rows: int, by_model: dict) -> dict:
        return {
            "agent": agent,
            "solved": n,
            "of": declared,
            "percent": _percent(n, declared),
            "tokens": total,
            "per_solved_task": _per_solved(total, n),
            "tokens_by_model": by_model,
            "attempt_rows": rows,
            "tokens_standing_trajectories_only": standing,
            "per_solved_task_standing_trajectories_only": _per_solved(standing, n),
        }

    def whole(side: Side, tasks_: Mapping[str, Mapping[str, Any]], model: str, agent: str) -> dict:
        n = sum(bool(task.get("solved")) for task in tasks_.values())
        total = int(side.projection["tokens"]["total"])
        standing = sum(_standing(task)[1] for task in tasks_.values())
        rows = int(side.projection["run"]["attempts"])
        return point(agent, n, total, standing, rows, {model: total})

    points = [
        whole(cheap, cheap_tasks, cheap_model, CHEAP_AGENT),
        whole(strong, strong_tasks, strong_model, STRONG_AGENT),
        point(
            AGENT,
            solved,
            c_tokens + s_tokens,
            c_standing + s_standing,
            sum(task["attempt_rows"]["cheap"] + task["attempt_rows"]["strong"] for task in tasks),
            {cheap_model: c_tokens, strong_model: s_tokens},
        ),
    ]
    for committed, computed in ((cheap, points[0]), (strong, points[1])):
        if committed.projection["tokens"]["per_solved_task"] != computed["per_solved_task"]:
            raise NotComposable(f"{computed['agent']}: cost per solved task does not reproduce")
    for this in points:
        this["dominated_by"] = [
            other["agent"]
            for other in points
            if other is not this
            and other["solved"] >= this["solved"]
            and other["tokens"] <= this["tokens"]
            and (other["solved"], other["tokens"]) != (this["solved"], this["tokens"])
        ]
    cascade, always_strong = points[2], points[1]
    strong_n, strong_total = always_strong["solved"], always_strong["tokens"]
    if solved and c_tokens:
        break_even: Any = round((strong_total * solved / strong_n - s_tokens) / c_tokens, 4)
    else:
        break_even = TBD

    def outcomes(ids: Sequence[str]) -> list[dict[str, Any]]:
        by_id = {task["task_id"]: task for task in tasks}
        keys = ("task_id", "db_id", "clauses", "cheap_solved", "strong_solved", "solved")
        return [{key: by_id[task_id][key] for key in keys} for task_id in ids]

    return {
        "measurement": {
            "agent": AGENT,
            "split": cheap.projection["measurement"]["split"],
            "tasks_declared": declared,
            "composed_from": {
                "cheap": _source(cheap, sources["cheap"]),
                "strong": _source(strong, sources["strong"]),
                "escalation": {
                    "file": sources["escalation"],
                    "run_id": escalation.get("run_id"),
                    "rule": "5.1, frozen at c1f8520 (constraint 88)",
                },
            },
            "note": (
                "Composed, not run: no request was made for this file. Every figure comes from "
                "the two runs named above, on their providers, models, dates and ledgers, and "
                "from the frozen rule's decisions on the cheap one."
            ),
        },
        "definitions": DEFINITIONS,
        "execution_accuracy": {
            "solved": solved,
            "of": declared,
            "percent": _percent(solved, declared),
            "empty_result_floor": {
                "tasks": len(empty_reference),
                "of": declared,
                "percent": _percent(len(empty_reference), declared),
            },
        },
        "escalation": {
            "escalated": len(escalated),
            "of": declared,
            "rate": round(len(escalated) / declared, 6),
            "clauses": {c: sum(c in task["clauses"] for task in tasks) for c in CLAUSES},
            "task_ids": [task["task_id"] for task in escalated],
        },
        "routes": {
            "rule_silent": {
                "tasks": len(silent),
                "cheap_solved": sum(task["cheap_solved"] for task in silent),
                "strong_solved": sum(task["strong_solved"] for task in silent),
            },
            "rule_fired": {
                "tasks": len(escalated),
                "cheap_solved": sum(task["cheap_solved"] for task in escalated),
                "strong_solved": sum(task["strong_solved"] for task in escalated),
            },
        },
        "paired": {
            "against_always_strong": _pairs(
                (t["solved"] for t in tasks), (t["strong_solved"] for t in tasks), ("A2", "A1")
            ),
            "against_always_cheap": _pairs(
                (t["solved"] for t in tasks),
                (t["cheap_solved"] for t in tasks),
                ("A2", "A2_cheap"),
            ),
        },
        "tokens": {
            "basis": "recorded attempt rows, attributed by task_id, retried attempts included",
            "by_model": {cheap_model: c_tokens, strong_model: s_tokens},
            "total": c_tokens + s_tokens,
            "per_declared_task": round((c_tokens + s_tokens) / declared, 1),
            "per_solved_task": cascade["per_solved_task"],
            "standing_trajectories_only": {
                "by_model": {cheap_model: c_standing, strong_model: s_standing},
                "total": c_standing + s_standing,
                "per_solved_task": cascade["per_solved_task_standing_trajectories_only"],
            },
            "refused_requests": {
                "cheap": sum(task["refused_requests"]["cheap"] for task in tasks),
                "strong": sum(task["refused_requests"]["strong"] for task in tasks),
            },
            "money": 0.0,
        },
        "frontier": {
            "points": points,
            "verdict": {
                "metric": "cost per solved task, recorded tokens",
                "cascade": cascade["per_solved_task"],
                "always_strong": always_strong["per_solved_task"],
                "cascade_wins": (c_tokens + s_tokens) * strong_n < strong_total * solved,
                "tasks_against_always_strong": solved - strong_n,
                "token_ratio_against_always_strong": round((c_tokens + s_tokens) / strong_total, 4),
            },
            "break_even_price_ratio": break_even,
        },
        "rows_that_could_move": {
            "empty_reference": outcomes(empty_reference),
            "verified_wrong_reference": outcomes(wrong_reference),
        },
        "tasks": tasks,
    }
