"""The reserve run: what it reads and writes, and the guard that lets it happen once.

The reserve set is read once, by one run, and by nothing else (`docs/SPLITS.md`). A0 runs it,
declared in `config/runs/reserve-set.toml`, through `scripts/a0_reserve.py`. How its figure is
read was committed in `docs/RESULTS.md` before the run. This module holds two things the
script should not be trusted to remember on its own:

- :func:`check_once` refuses a **second run ID** on the reserve set. Resuming the one run after
  a quota wall or a kill is part of that run, because a task with an answer is never re-run.
  A new run ID would be a second reading, and a reading cannot be taken back.
- :func:`compare` sets the reserve figure beside the working figure the way `docs/RESULTS.md`
  fixed before the run: the difference in tasks and in percentage points, computed from the
  counts; both empty-result floors; the figure without empty-reference tasks; the difficulty
  breakdown. **No threshold**, because each set has been run once and there is no measured
  spread to derive one from.

Nothing here opens `splits/reserve.json`. The guard reads the declared split out of each
ledger's ``run_start`` rows, which the run wrote.
"""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from query_pilot.run import read_rows
from query_pilot.run.ledger import DEFAULT_RUNS_ROOT, LEDGER_NAME

__all__ = [
    "DEFINITIONS",
    "DIFFICULTIES",
    "RESULTS",
    "RUN_CONFIG",
    "SPLIT",
    "SPLIT_FILE",
    "WORKING_RESULTS",
    "SecondReading",
    "check_once",
    "compare",
    "reserve_runs",
]

TBD: Final = "TBD"

#: The split name the run declares in ``[run.params]``, and what its projection is named after.
SPLIT: Final = "reserve"
#: Relative to the repository root. Only `scripts/a0_reserve.py` opens the first.
SPLIT_FILE: Final = Path("splits") / "reserve.json"
RUN_CONFIG: Final = Path("config") / "runs" / "reserve-set.toml"
RESULTS: Final = Path("results") / "a0-reserve.json"
#: What the reserve figure is set beside: A0 on the working set, 124 of 150.
WORKING_RESULTS: Final = Path("results") / "a0-working.json"

#: Spider's four, in the order the splits and the results page list them.
DIFFICULTIES: Final = ("easy", "medium", "hard", "extra")

DEFINITIONS: Final[Mapping[str, str]] = {
    "difference": (
        "Reserve minus working, in tasks and in percentage points, computed from the solved "
        "counts over the declared tasks and rounded to four places at the end."
    ),
    "excluding_empty_references": (
        "Solved tasks whose reference returns rows, over the declared tasks less the "
        "empty-result floor. A solve always executed its reference, so no row count is "
        "needed from a task the agent did not solve."
    ),
    "threshold": (
        "None. Each set has been run once and A0 runs at the provider's default temperature, "
        "so there is no measured spread to derive a threshold from. The difference is stated "
        "as a number with no adjective, beside the working figure, whichever way it lands."
    ),
    "limitation": (
        "Task generalisation, not schema generalisation. The same twenty databases appear in "
        "both sets, so the reserve figure answers whether the agent holds up on questions it "
        "has not been tuned against, not on databases it has never seen (docs/SPLITS.md)."
    ),
    "agreement": (
        "Matches the reference: the agent's rows equal the reference query's rows. Some "
        "references return wrong data themselves, so a match is agreement with the "
        "reference, not proof of a right answer."
    ),
}


class SecondReading(RuntimeError):
    """A start that would read the reserve set a second time."""


def reserve_runs(runs_root: Path | str = DEFAULT_RUNS_ROOT) -> list[str]:
    """Every run ID whose ledger declares the reserve split, in ID order, which is start order.

    Every ``run_start`` row is read rather than the first alone, so a segment that somehow
    declared a different split than the one before it still counts.
    """
    found: list[str] = []
    for ledger in sorted(Path(runs_root).glob(f"*/{LEDGER_NAME}")):
        for row in read_rows(ledger):
            if row.get("kind") != "run_start":
                continue
            params = (row.get("declared") or {}).get("params") or {}
            if params.get("split") == SPLIT:
                found.append(str(row.get("run_id") or ledger.parent.name))
                break
    return found


def check_once(
    run_id: str | None,
    *,
    runs_root: Path | str = DEFAULT_RUNS_ROOT,
    results: Path | str = RESULTS,
) -> None:
    """Refuse anything but the reserve run's first start, or a resume of that one run.

    ``run_id`` is ``None`` for a new start. A new start is refused once any ledger declares the
    reserve split, and once the committed projection exists, which covers a clone that has no
    ledgers. A given ``run_id`` is accepted only if it is that one run.
    """
    started = reserve_runs(runs_root)
    if len(started) > 1:
        raise SecondReading(
            f"the reserve set has already been read by {len(started)} runs "
            f"({', '.join(started)}). Stop and report; do not resume either."
        )
    if run_id is None:
        if started:
            raise SecondReading(
                f"the reserve set was read by run {started[0]}. Resume that run with "
                f"--run-id {started[0]}; a new run ID would be a second reading."
            )
        if Path(results).exists():
            raise SecondReading(
                f"{results} exists, so the reserve run has already been taken. A new run ID "
                f"would be a second reading."
            )
        return
    if run_id not in started:
        which = f"the reserve run is {started[0]}" if started else "no reserve run has started"
        raise SecondReading(
            f"run {run_id} is not the reserve run ({which}). Only that run may be resumed or "
            f"projected; a new run ID would be a second reading."
        )


def _share(solved: int, of: int) -> dict[str, Any]:
    return {"solved": solved, "of": of, "percent": round(100.0 * solved / of, 4) if of else TBD}


def _finished(document: Mapping[str, Any]) -> bool:
    """`project` writes the accuracy as a string, beginning TBD, when the run did not finish."""
    return not isinstance(document["execution_accuracy"]["percent"], str)


def _excluding_empty_references(document: Mapping[str, Any]) -> dict[str, Any] | str:
    accuracy = document["execution_accuracy"]
    floor = accuracy["empty_result_floor"]["tasks"]
    if not _finished(document) or isinstance(floor, str):
        return TBD
    solved = [task for task in document["tasks"] if task.get("solved")]
    if len(solved) != accuracy["solved"]:
        raise ValueError(
            f"{len(solved)} solved task rows against a solved count of {accuracy['solved']}"
        )
    unexecuted = [task["task_id"] for task in solved if task.get("reference_rows") is None]
    if unexecuted:
        raise ValueError(f"solved without a reference row count: {', '.join(unexecuted)}")
    on_empty = sum(1 for task in solved if task["reference_rows"] == 0)
    if on_empty > floor:
        raise ValueError(f"{on_empty} solves on empty references against a floor of {floor}")
    return _share(len(solved) - on_empty, accuracy["of"] - floor)


def _figure(document: Mapping[str, Any]) -> dict[str, Any]:
    measurement = document["measurement"]
    accuracy = document["execution_accuracy"]
    return {
        "run_id": measurement["run_id"],
        "ledger": measurement["ledger"],
        "date": measurement["date"],
        "provider_and_model": measurement["provider_and_model"],
        "solved": accuracy["solved"],
        "of": accuracy["of"],
        "percent": accuracy["percent"],
    }


def compare(reserve: Mapping[str, Any], working: Mapping[str, Any]) -> dict[str, Any]:
    """The reserve figure beside the working figure, as `docs/RESULTS.md` fixed before the run.

    Both are projections written by :func:`query_pilot.agents.project`. Refuses a pair that is
    not A0 on the reserve set beside A0 on the working set, and a working figure that is not
    finished. Everything that needs a finished reserve run reads ``TBD`` when it is not, and no
    rate over fewer tasks is produced.
    """
    for document, split in ((reserve, SPLIT), (working, "working")):
        measurement = document["measurement"]
        if measurement.get("split") != split or measurement.get("agent") != "A0":
            raise ValueError(
                f"expected A0 on the {split} set, got {measurement.get('agent')!r} on "
                f"{measurement.get('split')!r}"
            )
    if not _finished(working):
        raise ValueError("the working figure is not a finished run's")
    if reserve["execution_accuracy"]["of"] != working["execution_accuracy"]["of"]:
        raise ValueError("the two sets declare different numbers of tasks")

    finished = _finished(reserve)
    r_solved = reserve["execution_accuracy"]["solved"]
    w_solved = working["execution_accuracy"]["solved"]
    of = working["execution_accuracy"]["of"]
    if finished:
        points = 100 * (Fraction(r_solved, of) - Fraction(w_solved, of))
        difference: dict[str, Any] = {
            "tasks": r_solved - w_solved,
            "percentage_points": float(round(points, 4)),
        }
    else:
        difference = {"tasks": TBD, "percentage_points": TBD}

    def floor(document: Mapping[str, Any]) -> dict[str, Any]:
        block = document["execution_accuracy"]["empty_result_floor"]
        return {key: block[key] for key in ("tasks", "of", "percent")}

    def by_difficulty(document: Mapping[str, Any]) -> Any:
        levels = document.get("by_difficulty") or {}
        return {level: levels.get(level, TBD) for level in DIFFICULTIES}

    def tokens(document: Mapping[str, Any]) -> dict[str, Any]:
        block = document["tokens"]
        return {"total": block["total"], "per_solved_task": block["per_solved_task"]}

    return {
        "definitions": dict(DEFINITIONS),
        "working": _figure(working),
        "reserve": _figure(reserve),
        "difference": difference,
        "empty_result_floor": {"working": floor(working), "reserve": floor(reserve)},
        "excluding_empty_references": {
            "working": _excluding_empty_references(working),
            "reserve": _excluding_empty_references(reserve),
        },
        "by_difficulty": {
            "working": by_difficulty(working),
            "reserve": by_difficulty(reserve) if finished else TBD,
        },
        "reasons": {
            "working": dict(working["reasons"]),
            "reserve": dict(reserve["reasons"]) if finished else TBD,
        },
        "tokens": {"working": tokens(working), "reserve": tokens(reserve)},
        "threshold": None,
    }
