"""The reserve run happens once, and its figure is read the way `docs/RESULTS.md` fixed first.

Two things are checked here before any reserve figure exists. The guard: a second run ID on the
reserve set is refused, whatever state the ledgers are in, and resuming the one run is not. The
comparison: the arithmetic that sets the reserve figure beside A0's working figure, run on the
committed working projection and on reserve projections made from it.

**Nothing here opens `splits/reserve.json`**, and no test makes a live API call. The ledgers are
written by the real run machinery into a temporary directory, with stub tasks.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from query_pilot import reserve
from query_pilot.agents import results_name
from query_pilot.run import Run, RunConfig, RunLedger, TaskResult

REPO = Path(__file__).resolve().parents[1]


def load(relative: Path | str) -> dict:
    return json.loads((REPO / relative).read_text(encoding="utf-8"))


WORKING = load(reserve.WORKING_RESULTS)


# -- the files the run reads and writes ------------------------------------------------------------


def test_the_run_reads_and_writes_the_files_the_reading_names() -> None:
    assert reserve.SPLIT_FILE.as_posix() == "splits/reserve.json"
    assert reserve.RUN_CONFIG.as_posix() == "config/runs/reserve-set.toml"
    assert reserve.RESULTS.as_posix() == "results/a0-reserve.json"
    assert (REPO / reserve.RUN_CONFIG).exists()
    # The projection is named after what it declares, so the script cannot write it elsewhere.
    measurement = {"agent": "A0", "split": reserve.SPLIT}
    assert results_name({"measurement": measurement}) == reserve.RESULTS.name
    reading = (REPO / "docs" / "RESULTS.md").read_text(encoding="utf-8")
    for path in (reserve.SPLIT_FILE, reserve.RUN_CONFIG, reserve.RESULTS):
        assert f"`{path.as_posix()}`" in reading


def test_the_run_declares_the_split_the_guard_looks_for() -> None:
    config = RunConfig.load(REPO / reserve.RUN_CONFIG, ["t-1"], run_id="r-1")
    assert config.agent == "A0" and config.params == {"split": reserve.SPLIT}


# -- the guard -------------------------------------------------------------------------------------


async def answer(context) -> TaskResult:
    return TaskResult()


async def start(runs: Path, run_id: str, split: str, *, tasks: int = 2) -> None:
    """A real ledger, written by the real run machinery, declaring ``split``."""
    config = RunConfig.start(
        "A0",
        [f"t-{i}" for i in range(tasks)],
        run_id=run_id,
        token_ceiling=1_000,
        wall_clock_ceiling_s=60.0,
        params={"split": split},
    )
    ledger = RunLedger(run_id, root=runs)
    with ledger:
        await Run(config, ledger).execute(answer)


def test_nothing_started_admits_the_first_start(tmp_path) -> None:
    runs = tmp_path / "runs"
    assert reserve.reserve_runs(runs) == []
    reserve.check_once(None, runs_root=runs, results=tmp_path / "a0-reserve.json")


async def test_working_set_runs_do_not_count_as_the_reserve_run(tmp_path) -> None:
    runs = tmp_path / "runs"
    await start(runs, "20260908-000000-aaaaaa", "working")
    await start(runs, "20260912-000000-bbbbbb", "smoke")
    assert reserve.reserve_runs(runs) == []
    reserve.check_once(None, runs_root=runs, results=tmp_path / "a0-reserve.json")


async def test_once_the_reserve_run_exists_a_new_start_is_refused_and_its_resume_is_not(
    tmp_path,
) -> None:
    runs = tmp_path / "runs"
    await start(runs, "20260908-000000-aaaaaa", "working")
    await start(runs, "20260913-000000-cccccc", "reserve")
    results = tmp_path / "a0-reserve.json"

    assert reserve.reserve_runs(runs) == ["20260913-000000-cccccc"]
    with pytest.raises(reserve.SecondReading, match=r"--run-id 20260913-000000-cccccc"):
        reserve.check_once(None, runs_root=runs, results=results)
    # Resuming or projecting the one run is part of it.
    reserve.check_once("20260913-000000-cccccc", runs_root=runs, results=results)
    # Any other run ID is not, including a working-set run's and one nobody has used.
    for other in ("20260908-000000-aaaaaa", "20260913-111111-dddddd"):
        with pytest.raises(reserve.SecondReading, match="is not the reserve run"):
            reserve.check_once(other, runs_root=runs, results=results)


async def test_a_resume_is_still_admitted_after_the_run_wrote_its_projection(tmp_path) -> None:
    runs = tmp_path / "runs"
    await start(runs, "20260913-000000-cccccc", "reserve")
    results = tmp_path / "a0-reserve.json"
    results.write_text("{}")
    reserve.check_once("20260913-000000-cccccc", runs_root=runs, results=results)
    with pytest.raises(reserve.SecondReading):
        reserve.check_once(None, runs_root=runs, results=results)


def test_a_committed_projection_refuses_a_new_start_where_there_are_no_ledgers(tmp_path) -> None:
    """A fresh clone has `results/a0-reserve.json` and no `runs/`, and must not run again."""
    results = tmp_path / "a0-reserve.json"
    results.write_text("{}")
    with pytest.raises(reserve.SecondReading, match="already been taken"):
        reserve.check_once(None, runs_root=tmp_path / "runs", results=results)


def test_a_run_id_is_refused_when_no_reserve_run_has_started(tmp_path) -> None:
    """`--run-id` resumes; it never names the first run, which would skip the guard's check."""
    with pytest.raises(reserve.SecondReading, match="no reserve run has started"):
        reserve.check_once(
            "20260913-000000-cccccc", runs_root=tmp_path / "runs", results=tmp_path / "x.json"
        )


async def test_two_reserve_runs_refuse_everything(tmp_path) -> None:
    runs = tmp_path / "runs"
    await start(runs, "20260913-000000-cccccc", "reserve")
    await start(runs, "20260913-000001-eeeeee", "reserve")
    for run_id in (None, "20260913-000000-cccccc", "20260913-000001-eeeeee"):
        with pytest.raises(reserve.SecondReading, match="2 runs"):
            reserve.check_once(run_id, runs_root=runs, results=tmp_path / "a0-reserve.json")


async def test_every_segment_is_read_and_a_torn_last_line_is_not_fatal(tmp_path) -> None:
    runs = tmp_path / "runs"
    await start(runs, "20260913-000000-cccccc", "working")
    ledger = runs / "20260913-000000-cccccc" / "ledger.jsonl"
    # A later segment declaring the reserve split, then a line cut off by a kill.
    row = {"kind": "run_start", "run_id": "20260913-000000-cccccc"}
    row["declared"] = {"params": {"split": "reserve"}}
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n" + '{"kind": "attempt", "run_id": "2026')
    assert reserve.reserve_runs(runs) == ["20260913-000000-cccccc"]


# -- the comparison --------------------------------------------------------------------------------


def as_reserve(document: dict, *, unsolve: int = 0, unsolve_on_empty: int = 0) -> dict:
    """The working projection relabelled as a reserve one, with some of its solves taken away.

    Keeps every count consistent with the task rows, the way `project` writes them.
    """
    made = copy.deepcopy(document)
    made["measurement"]["split"] = reserve.SPLIT
    made["measurement"]["run_id"] = "20260913-000000-cccccc"
    solved = [t for t in made["tasks"] if t.get("solved")]
    chosen = [t for t in solved if t["reference_rows"] > 0][:unsolve]
    chosen += [t for t in solved if t["reference_rows"] == 0][:unsolve_on_empty]
    for task in chosen:
        task["solved"] = False
        task["reason"] = "value_mismatch"
        made["reasons"]["solved"] -= 1
        made["reasons"]["value_mismatch"] += 1
        made["by_difficulty"][task["difficulty"]]["solved"] -= 1
    accuracy = made["execution_accuracy"]
    accuracy["solved"] -= len(chosen)
    accuracy["percent"] = round(100.0 * accuracy["solved"] / accuracy["of"], 4)
    return made


def test_the_working_figure_beside_itself_differs_by_nothing() -> None:
    compared = reserve.compare(as_reserve(WORKING), WORKING)
    assert compared["difference"] == {"tasks": 0, "percentage_points": 0.0}
    assert compared["threshold"] is None
    assert compared["definitions"] == dict(reserve.DEFINITIONS)


def test_the_working_side_is_the_figures_the_reading_committed() -> None:
    """The numbers `docs/RESULTS.md` wrote beside the reserve's TBD, recomputed from the file."""
    compared = reserve.compare(as_reserve(WORKING), WORKING)
    working = compared["working"]
    assert (working["solved"], working["of"], working["percent"]) == (124, 150, 82.6667)
    assert working["run_id"] == "20260908-133316-faecd5"
    assert working["ledger"] == "runs/20260908-133316-faecd5/ledger.jsonl"
    assert compared["empty_result_floor"]["working"] == {"tasks": 7, "of": 150, "percent": 4.6667}
    assert compared["excluding_empty_references"]["working"] == {
        "solved": 117,
        "of": 143,
        "percent": 81.8182,
    }
    levels = compared["by_difficulty"]["working"]
    assert [(levels[d]["solved"], levels[d]["of"]) for d in reserve.DIFFICULTIES] == [
        (33, 36),
        (53, 65),
        (19, 25),
        (19, 24),
    ]
    assert compared["reasons"]["working"] == {
        "solved": 124,
        "value_mismatch": 13,
        "row_count": 11,
        "column_count": 2,
    }
    assert compared["tokens"]["working"] == {"total": 106_740, "per_solved_task": 860.8}
    reading = (REPO / "docs" / "RESULTS.md").read_text(encoding="utf-8")
    for line in ("| 124 of 150 — 82.6667% |", "| 7 of 150 — 4.6667% |", "| 117 of 143 |"):
        assert line in reading


@pytest.mark.parametrize(
    ("unsolve", "tasks", "points"),
    [(1, -1, -0.6667), (4, -4, -2.6667), (8, -8, -5.3333), (24, -24, -16.0)],
)
def test_the_difference_is_reserve_minus_working_from_the_counts(unsolve, tasks, points) -> None:
    compared = reserve.compare(as_reserve(WORKING, unsolve=unsolve), WORKING)
    assert compared["difference"] == {"tasks": tasks, "percentage_points": points}
    assert compared["reserve"]["solved"] == 124 - unsolve


def test_a_reserve_figure_above_the_working_one_is_a_positive_difference() -> None:
    """Swapped, so the reserve side is the larger: the sign is the direction, and nothing else."""
    lower = as_reserve(WORKING, unsolve=3)
    higher = copy.deepcopy(WORKING)
    higher["measurement"]["split"] = reserve.SPLIT
    lower["measurement"]["split"] = "working"
    compared = reserve.compare(higher, lower)
    assert compared["difference"] == {"tasks": 3, "percentage_points": 2.0}


def test_excluding_empty_references_moves_only_with_solves_on_rows() -> None:
    on_rows = reserve.compare(as_reserve(WORKING, unsolve=2), WORKING)
    assert on_rows["excluding_empty_references"]["reserve"]["solved"] == 115
    on_empty = reserve.compare(as_reserve(WORKING, unsolve_on_empty=2), WORKING)
    assert on_empty["excluding_empty_references"]["reserve"] == {
        "solved": 117,
        "of": 143,
        "percent": 81.8182,
    }
    assert on_empty["difference"]["tasks"] == -2


def test_an_unfinished_reserve_run_produces_no_rate_over_fewer_tasks() -> None:
    unfinished = as_reserve(WORKING, unsolve=5)
    unfinished["execution_accuracy"]["percent"] = "TBD (run incomplete: token_ceiling)"
    compared = reserve.compare(unfinished, WORKING)
    assert compared["difference"] == {"tasks": "TBD", "percentage_points": "TBD"}
    assert compared["excluding_empty_references"]["reserve"] == "TBD"
    assert compared["by_difficulty"]["reserve"] == "TBD"
    assert compared["reasons"]["reserve"] == "TBD"
    assert compared["reserve"]["percent"].startswith("TBD")
    # The working side is still there to be read beside it.
    assert compared["working"]["percent"] == 82.6667


def test_it_refuses_a_pair_that_is_not_a0_reserve_beside_a0_working() -> None:
    with pytest.raises(ValueError, match="reserve set"):
        reserve.compare(WORKING, WORKING)
    a1 = as_reserve(WORKING)
    a1["measurement"]["agent"] = "A1"
    with pytest.raises(ValueError, match="'A1'"):
        reserve.compare(a1, WORKING)
    unfinished = copy.deepcopy(WORKING)
    unfinished["execution_accuracy"]["percent"] = "TBD (run incomplete: killed)"
    with pytest.raises(ValueError, match="not a finished run"):
        reserve.compare(as_reserve(WORKING), unfinished)
    shorter = as_reserve(WORKING)
    shorter["execution_accuracy"]["of"] = 149
    with pytest.raises(ValueError, match="different numbers of tasks"):
        reserve.compare(shorter, WORKING)


def test_a_solve_without_a_reference_row_count_is_refused_rather_than_guessed() -> None:
    broken = as_reserve(WORKING)
    next(t for t in broken["tasks"] if t.get("solved"))["reference_rows"] = None
    with pytest.raises(ValueError, match="without a reference row count"):
        reserve.compare(broken, WORKING)


def test_the_limitation_and_the_missing_threshold_travel_with_the_figure() -> None:
    definitions = reserve.DEFINITIONS
    assert "not schema generalisation" in definitions["limitation"]
    assert "not proof of a right answer" in definitions["agreement"]
    assert definitions["threshold"].startswith("None.")
    splits = (REPO / "docs" / "SPLITS.md").read_text(encoding="utf-8")
    assert "task generalisation, not schema generalisation" in splits
