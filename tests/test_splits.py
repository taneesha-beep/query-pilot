"""Checks on the working set, the reserve set and the smoke set.

All but the last read committed files only, so they run with no substrate, no network and
no keys. The two that matter most are disjointness and reproducibility: the first protects
the reserve set from contamination, the second makes "regenerable from the seed" a claim
with a test behind it rather than a sentence in a document.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from query_pilot.difficulty import DIFFICULTIES, difficulty_of
from query_pilot.splits import (
    RESERVE_SIZE,
    SEED,
    SMOKE_SIZE,
    WORKING_SIZE,
    build_splits,
    largest_remainder,
    marginals,
)

REPO = Path(__file__).resolve().parent.parent
SPLITS = REPO / "splits"
SPIDER = REPO / "data" / "spider"

FRAME = json.loads((SPLITS / "frame.json").read_text())
TASKS = FRAME["tasks"]
COMMITTED = {
    name: json.loads((SPLITS / f"{name}.json").read_text())
    for name in ("working", "reserve", "smoke")
}

needs_substrate = pytest.mark.skipif(
    not (SPIDER / "dev.json").exists(),
    reason="substrate not acquired; see docs/SUBSTRATE.md",
)


def test_the_sets_are_the_declared_sizes() -> None:
    assert len(COMMITTED["working"]["task_ids"]) == WORKING_SIZE
    assert len(COMMITTED["reserve"]["task_ids"]) == RESERVE_SIZE
    assert len(COMMITTED["smoke"]["task_ids"]) == SMOKE_SIZE
    for name, contents in COMMITTED.items():
        assert contents["count"] == len(contents["task_ids"]), name
        assert len(set(contents["task_ids"])) == contents["count"], f"{name} repeats a task"


def test_the_working_set_and_the_reserve_set_do_not_intersect() -> None:
    working = set(COMMITTED["working"]["task_ids"])
    reserve = set(COMMITTED["reserve"]["task_ids"])
    assert working & reserve == set()


def test_the_smoke_set_lies_inside_the_working_set() -> None:
    # The smoke set is for development iteration. It is drawn from the working set, so it
    # cannot leak the reserve set, and no number taken on it is ever reported.
    assert set(COMMITTED["smoke"]["task_ids"]) <= set(COMMITTED["working"]["task_ids"])


def test_every_drawn_task_is_in_the_frame() -> None:
    for name, contents in COMMITTED.items():
        unknown = set(contents["task_ids"]) - set(TASKS)
        assert unknown == set(), f"{name} draws tasks outside the frame: {sorted(unknown)[:5]}"


def test_the_two_sets_carry_the_same_difficulty_mix() -> None:
    working = marginals(COMMITTED["working"]["task_ids"], TASKS, "difficulty")
    reserve = marginals(COMMITTED["reserve"]["task_ids"], TASKS, "difficulty")
    assert working == reserve
    assert sorted(working) == sorted(DIFFICULTIES)


def test_every_database_in_the_frame_reaches_both_sets() -> None:
    frame_databases = {entry["db_id"] for entry in TASKS.values()}
    for name in ("working", "reserve"):
        drawn = {TASKS[task_id]["db_id"] for task_id in COMMITTED[name]["task_ids"]}
        assert drawn == frame_databases, f"{name} is missing {sorted(frame_databases - drawn)}"


def test_the_committed_sets_regenerate_from_the_seed() -> None:
    rebuilt = build_splits(TASKS, seed=SEED)
    for name, contents in COMMITTED.items():
        assert rebuilt[name] == contents["task_ids"], (
            f"{name}.json does not match a fresh draw at seed {SEED}. Either the file was "
            "edited by hand or the draw changed; both need a decision entry, not a rerun."
        )
        assert contents["seed"] == SEED


def test_a_different_seed_draws_different_sets() -> None:
    other = build_splits(TASKS, seed=SEED + 1)
    assert other["working"] != COMMITTED["working"]["task_ids"]
    assert other["reserve"] != COMMITTED["reserve"]["task_ids"]


def test_largest_remainder_apportions_exactly() -> None:
    assert sum(largest_remainder(150, {"a": 248, "b": 446, "c": 174, "d": 166}).values()) == 150
    assert largest_remainder(0, {"a": 1, "b": 2}) == {"a": 0, "b": 0}
    assert largest_remainder(5, {"a": 0, "b": 0}) == {"a": 0, "b": 0}
    # Equal weights split as evenly as they can, and the remainder goes by key order.
    assert largest_remainder(3, {"a": 10, "b": 10}) == {"a": 2, "b": 1}


@needs_substrate
def test_the_frame_matches_the_substrate() -> None:
    dev = json.loads((SPIDER / "dev.json").read_text())
    assert (
        hashlib.sha256((SPIDER / "dev.json").read_bytes()).hexdigest() == FRAME["dev_json_sha256"]
    )
    for index, item in enumerate(dev):
        task_id = f"dev-{index:04d}"
        if task_id not in TASKS:
            continue
        assert TASKS[task_id]["db_id"] == item["db_id"]
        assert TASKS[task_id]["difficulty"] == difficulty_of(item["sql"])
