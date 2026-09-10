"""A run's ledger, projected into the one file about it that gets committed.

**This is a projection, not a second source.** Every figure in it is derived from the
ledger by reading the ledger, and the ledger stays the record: delete the projection and
it can be rebuilt; edit the projection by hand and it is wrong. It carries the run ID and
the ledger path it came from so that the two can always be put back beside each other.

It lives in `agents/` rather than in `run/` for the reason the whole seam exists: this
module looks **inside** a task row's ``detail``, and `run/` never does. A run records that
a task produced an answer; what the answer was worth is the agent's business and this is
where that is read back out.

**Nothing here decides what a solve is.** `equivalence` decided that before any result
existed and A0 wrote the verdict into the row. This counts what is already written down,
and a projection that recomputed a verdict would be a second measurement instrument
disagreeing quietly with the first.

Two refusals, both the same rule the run summary already applies in 1.3:

- **No rate is written unless every declared task has an outcome.** A run with three tasks
  unanswered has no accuracy figure — 147 solved out of 150 declared is not a percentage of
  anything, and 147 out of 147 is a different experiment. Where a rate cannot be computed
  the file says ``TBD`` and why, which is this project's placeholder convention.
- **The empty-result floor is written beside the accuracy figure, not under it.**
  `docs/EQUIVALENCE.md` records that a query returning nothing scores a measurable share of
  a split for free, and says that share belongs beside every accuracy number this project
  reports rather than in a footnote. This is where that is kept or quietly broken.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from query_pilot.run.ledger import COMPLETE, LEDGER_NAME, OK, read_rows

__all__ = ["project", "results_name", "write_results"]

#: This project's placeholder. A figure that could not be computed says so.
TBD = "TBD"

#: The keys of a task row's ``detail`` that are carried through per task, in this order.
#: The exception *message* is deliberately not among them: it is diagnostic, it lives in
#: the gitignored ledger, and a committed file is the wrong place for a provider's prose.
_CARRIED = (
    "db_id",
    "solved",
    "reason",
    "detail",
    "sql",
    "reference_sql",
    "fenced",
    "dropped_statements",
    "finish_reason",
    "provider",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "latency_s",
    "candidate_rows",
    "candidate_seconds",
    "candidate_truncated_by",
    "candidate_timed_out",
    "reference_rows",
    # A1's own. Absent from an A0 row, which is why every key here is copied only when the
    # detail holds it: one projection serves both agents and neither is asked to carry the
    # other's fields. `validation_rule` is here because `no_sql` means two different things
    # for A1 -- no statement in the answer, or a reply the 3.3 validator rejected -- and a
    # committed file that dropped it would leave nothing to group those apart by.
    "termination",
    "turns",
    "tool_calls",
    "tool_calls_by_name",
    "turn_limit",
    "tool_call_limit",
    "repair_attempts",
    "repair_succeeded",
    "repair_blocked",
    "validation_rule",
    "transcript",
)


def _percent(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 4)


def project(
    ledger: Path | str,
    *,
    split: str | None = None,
    difficulty: Mapping[str, str] | None = None,
    empty_reference_tasks: int | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Fold one run's ledger into the committed result shape.

    ``difficulty`` and ``empty_reference_tasks`` are facts about the substrate rather than
    about the run, so they are passed in by the caller that has the substrate open. Both
    are optional and both read ``TBD`` when absent, because a figure this function did not
    receive is one it must not invent.
    """
    source = Path(ledger)
    if source.is_dir():
        source = source / LEDGER_NAME

    run_id: str | None = None
    agent: str | None = None
    declared: list[str] = []
    started_at: str | None = None
    ended_at: str | None = None
    incomplete_reason: str | None = None
    status: str | None = None
    attempts = 0
    prompt_tokens = completion_tokens = 0
    models: Counter[str] = Counter()
    models_returned: Counter[str] = Counter()
    error_classes: Counter[str] = Counter()
    tasks: dict[str, dict[str, Any]] = {}

    for row in read_rows(source):
        kind = row.get("kind")
        run_id = run_id or row.get("run_id")
        if kind == "run_start":
            agent = row.get("agent") or agent
            started_at = started_at or row.get("started_at")
            declared = list((row.get("declared") or {}).get("task_ids") or declared)
        elif kind == "run_end":
            ended_at = row.get("ended_at")
            status = row.get("status")
            incomplete_reason = row.get("incomplete_reason")
        elif kind == "attempt":
            attempts += 1
            prompt_tokens += row.get("prompt_tokens") or 0
            completion_tokens += row.get("completion_tokens") or 0
            if row.get("outcome") == OK:
                models[f"{row.get('provider')}/{row.get('model')}"] += 1
                if row.get("model_returned"):
                    models_returned[str(row["model_returned"])] += 1
            else:
                error_classes[str(row.get("error_class") or "unclassified")] += 1
        elif kind == "task":
            task_id = str(row.get("task_id"))
            detail = row.get("detail") or {}
            record: dict[str, Any] = {"task_id": task_id, "status": row.get("status")}
            if difficulty is not None and task_id in difficulty:
                record["difficulty"] = difficulty[task_id]
            for key in _CARRIED:
                if key in detail:
                    record[key] = detail[key]
            if row.get("status") != COMPLETE:
                # A task with no answer. `solved` is absent rather than false: the model
                # never got to be wrong, and a false here would put an infrastructure
                # failure into the accuracy figure's numerator's denominator.
                record["error_class"] = row.get("error_class")
                record["exception"] = detail.get("exception")
            record["elapsed_s"] = row.get("elapsed_s")
            tasks[task_id] = record

    ordered = [tasks[t] for t in declared if t in tasks] + [
        record for task_id, record in tasks.items() if task_id not in declared
    ]
    complete = [r for r in ordered if r["status"] == COMPLETE]
    solved = [r for r in complete if r.get("solved")]
    total_tokens = prompt_tokens + completion_tokens
    answered_all = bool(declared) and len(complete) == len(declared)
    finished = status == "complete" and incomplete_reason is None and answered_all

    if finished:
        accuracy: Any = _percent(len(solved), len(declared))
        per_solved: Any = round(total_tokens / len(solved), 1) if solved else TBD
    else:
        why = incomplete_reason or (
            f"{len(complete)} of {len(declared)} declared tasks have an outcome"
        )
        accuracy = f"{TBD} (run incomplete: {why})"
        per_solved = accuracy

    if empty_reference_tasks is None:
        floor: dict[str, Any] = {"tasks": TBD, "of": len(declared), "percent": TBD}
    else:
        floor = {
            "tasks": empty_reference_tasks,
            "of": len(declared),
            "percent": _percent(empty_reference_tasks, len(declared)) if declared else TBD,
        }
    floor["what_it_means"] = (
        "Reference queries in this split that return no rows. An empty candidate result "
        "matching an empty reference is a solve (docs/EQUIVALENCE.md, decision 6), so this "
        "is what an agent scores by answering nothing at all. It belongs beside the "
        "accuracy figure above rather than in a footnote."
    )

    reasons = Counter(str(r.get("reason")) for r in complete)
    # Two aggregates that only an agent with a trajectory can produce. Emitted only when
    # something produced them, so a single-shot run's projection is unchanged by their
    # existence rather than carrying two empty mappings that mean nothing about it.
    terminations = Counter(str(r["termination"]) for r in complete if r.get("termination"))
    validation_rules = Counter(
        str(r["validation_rule"]) for r in complete if r.get("validation_rule")
    )
    document: dict[str, Any] = {
        "measurement": {
            "agent": agent,
            "split": split,
            "tasks_declared": len(declared),
            "provider_and_model": sorted(models),
            "model_returned": sorted(models_returned),
            "date": (started_at or "")[:10] or TBD,
            "started_at": started_at,
            "ended_at": ended_at,
            "run_id": run_id,
            "ledger": str(source),
            "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
            "note": (
                "Derived from the ledger named above and regenerable from it. Every figure "
                "in this file was produced by that run, on that provider and model, on that "
                "date."
            ),
        },
        "run": {
            "status": status,
            "incomplete_reason": incomplete_reason,
            "tasks_complete": len(complete),
            "tasks_failed": len(ordered) - len(complete),
            "attempts": attempts,
            "error_classes": dict(error_classes),
        },
        "execution_accuracy": {
            "solved": len(solved),
            "of": len(declared),
            "percent": accuracy,
            "empty_result_floor": floor,
        },
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": total_tokens,
            "per_declared_task": round(total_tokens / len(declared), 1) if declared else TBD,
            "per_solved_task": per_solved,
            "money": 0.0,
            "note": (
                "Tokens from every attempt, answered or refused, because that is what was "
                "spent. The money figure is zero: both providers are free tiers and no paid "
                "API spend is permitted anywhere in this project."
            ),
        },
        "reasons": dict(reasons.most_common()),
        "sandbox": {
            "candidates_truncated": sum(1 for r in complete if r.get("candidate_truncated_by")),
            "candidates_timed_out": sum(1 for r in complete if r.get("candidate_timed_out")),
            "completions_stopped_at_length": sum(
                1 for r in complete if r.get("finish_reason") == "length"
            ),
        },
        "tasks": ordered,
    }

    if terminations:
        document["terminations"] = dict(terminations.most_common())
    if validation_rules:
        # Which of 3.3's three rules rejected a reply. `no_sql` is the equivalence rule's
        # verdict on all of them and never says which, by design: no ninth slug was added
        # underneath a committed result. This is where the distinction lives instead.
        document["validation_rules"] = dict(validation_rules.most_common())

    if difficulty is not None:
        by_difficulty: dict[str, dict[str, Any]] = {}
        for record in complete:
            level = str(record.get("difficulty", TBD))
            bucket = by_difficulty.setdefault(level, {"solved": 0, "of": 0})
            bucket["of"] += 1
            bucket["solved"] += bool(record.get("solved"))
        for bucket in by_difficulty.values():
            bucket["percent"] = _percent(bucket["solved"], bucket["of"]) if bucket["of"] else TBD
        document["by_difficulty"] = dict(sorted(by_difficulty.items()))

    return document


def results_name(document: Mapping[str, Any]) -> str:
    """What a projection is called, read off the projection itself.

    ``a0-working.json``, ``a1-working.json``. The agent and the split are the two things
    that make one committed result a different measurement from another, and both are
    already inside the document because they were declared before the run started.

    It is derived rather than passed in for the reason the whole module is agent-agnostic:
    nothing here learns which agent wrote a task row's ``detail``, and a constant naming one
    agent would be the single line that did. A document missing either field raises rather
    than being given a plausible name, because a file called ``none-none.json`` is worse
    than a refusal.
    """
    measurement = document.get("measurement") or {}
    agent = measurement.get("agent")
    split = measurement.get("split")
    if not agent or not split:
        raise ValueError(
            "a projection is named after the agent and the split it declared, and this one "
            f"carries agent={agent!r} split={split!r}. Pass an explicit filename instead."
        )
    return f"{str(agent).lower()}-{str(split).lower()}.json"


def write_results(document: Mapping[str, Any], path: Path | str) -> Path:
    """Write a projection. Takes the file, or the directory it belongs in."""
    target = Path(path)
    if target.is_dir() or not target.suffix:
        target = target / results_name(document)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2) + "\n")
    return target
