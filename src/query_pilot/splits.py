"""Draw the working set, the reserve set and the smoke set from the sampling frame.

Three sets, one seed, no overlap between the two that matter:

- **working set**, 150 tasks. Every intermediate number in this project comes from here.
- **reserve set**, 150 tasks, disjoint from the working set. Read once, at the very end.
- **smoke set**, 15 tasks drawn *from within* the working set. Development iteration only.
  No number taken on it is ever reported. It exists so that debugging an agent loop costs
  15 tasks of a day's quota instead of 150.

**Stratification.** Difficulty is the stratum, because difficulty is the dimension
accuracy is reported by. The working and reserve allocations across difficulty are
identical by construction, so the two sets carry the same difficulty mix exactly rather
than approximately.

**Balance.** `db_id` is a balance constraint inside each difficulty, not a stratum. Twenty
databases crossed with four difficulties would give eighty cells over a 300-task draw,
and cells that small make an allocation that is mostly rounding. Instead each difficulty's
draw is spread across databases proportionally, then split between the two sets by
alternating within each cell, which keeps the per-database counts within one of each other.

Determinism: every collection is sorted before it is shuffled, and one seeded generator is
consumed in a fixed order.
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Any

from query_pilot.difficulty import DIFFICULTIES

SEED = 20260906
WORKING_SIZE = 150
RESERVE_SIZE = 150
SMOKE_SIZE = 15


def largest_remainder(total: int, weights: dict[str, int]) -> dict[str, int]:
    """Apportion `total` across keys in proportion to `weights`.

    Ties on the fractional part are broken by the key order given, so the result depends
    on nothing but the arguments.
    """
    pool = sum(weights.values())
    if pool == 0:
        return dict.fromkeys(weights, 0)
    exact = {key: total * weight / pool for key, weight in weights.items()}
    allocation = {key: int(value) for key, value in exact.items()}
    short = total - sum(allocation.values())
    order = sorted(
        weights, key=lambda key: (-(exact[key] - allocation[key]), list(weights).index(key))
    )
    for key in order[:short]:
        allocation[key] += 1
    return allocation


def _apportion_within_capacity(total: int, capacity: dict[str, int]) -> dict[str, int]:
    """Largest-remainder apportionment that never exceeds a cell's available tasks."""
    allocation = largest_remainder(total, capacity)
    overflow = 0
    for key, share in allocation.items():
        if share > capacity[key]:
            overflow += share - capacity[key]
            allocation[key] = capacity[key]
    while overflow > 0:
        room = {k: capacity[k] - allocation[k] for k in allocation if capacity[k] > allocation[k]}
        if not room:
            raise ValueError("cannot apportion: demand exceeds the frame")
        # Give the next task to the cell with the most unused capacity, ties by key order.
        target = max(room, key=lambda k: (room[k], -list(capacity).index(k)))
        allocation[target] += 1
        overflow -= 1
    return allocation


def _pair_singletons(allocation: dict[str, int], capacity: dict[str, int]) -> dict[str, int]:
    """Give any database drawn from at all at least two tasks, so both sets can hold one.

    A database with four questions in all of dev is apportioned a single task at this
    sample size, which puts it in one set and not the other and leaves the two sets
    measuring different numbers of databases. Lifting singletons to a pair costs one task
    from the largest cell and buys comparability, which is the reserve set's whole purpose.
    A database with only one task available stays at one; nothing can be done for it.
    """
    allocation = dict(allocation)
    while True:
        singletons = [
            key for key in sorted(allocation) if allocation[key] == 1 and capacity[key] >= 2
        ]
        if not singletons:
            return allocation
        donors = [key for key in sorted(allocation) if allocation[key] >= 3]
        if not donors:
            return allocation
        donor = max(donors, key=lambda k: (allocation[k], -list(capacity).index(k)))
        allocation[donor] -= 1
        allocation[singletons[0]] += 1


def build_splits(frame: dict[str, dict[str, str]], seed: int = SEED) -> dict[str, list[str]]:
    """Return the three sets as sorted task ID lists.

    `frame` maps task ID to `{"db_id": ..., "difficulty": ...}` and is the set of tasks
    whose reference query executes. See docs/SUBSTRATE.md.
    """
    rng = random.Random(seed)

    by_difficulty: dict[str, dict[str, list[str]]] = {d: defaultdict(list) for d in DIFFICULTIES}
    for task_id in sorted(frame):
        entry = frame[task_id]
        by_difficulty[entry["difficulty"]][entry["db_id"]].append(task_id)

    available = {d: sum(len(v) for v in by_difficulty[d].values()) for d in DIFFICULTIES}
    per_set = largest_remainder(WORKING_SIZE, available)
    if RESERVE_SIZE != WORKING_SIZE:
        raise ValueError("the two sets share one allocation, so they must be the same size")
    for difficulty, share in per_set.items():
        if 2 * share > available[difficulty]:
            raise ValueError(
                f"{difficulty}: need {2 * share} tasks, frame has {available[difficulty]}"
            )

    working: list[str] = []
    reserve: list[str] = []
    for difficulty in DIFFICULTIES:
        cells = by_difficulty[difficulty]
        capacity = {db_id: len(cells[db_id]) for db_id in sorted(cells)}
        draw = _apportion_within_capacity(2 * per_set[difficulty], capacity)
        draw = _pair_singletons(draw, capacity)
        working_takes_the_extra = True
        for db_id in sorted(cells):
            candidates = sorted(cells[db_id])
            rng.shuffle(candidates)
            taken = candidates[: draw[db_id]]
            # Deal alternately, so the two sets stay within one task of each other in
            # every cell. An odd cell has one task left over; hand it to each set in turn
            # rather than always to the working set, or the working set finishes well over
            # its allocation and correcting that skews the database spread.
            first, second = (working, reserve) if working_takes_the_extra else (reserve, working)
            first.extend(taken[0::2])
            second.extend(taken[1::2])
            if draw[db_id] % 2:
                working_takes_the_extra = not working_takes_the_extra

    # A cell with an odd draw hands the extra task to the working set, so the working set
    # can finish over its difficulty allocation and the reserve set under it. Even them up
    # by moving whole tasks, taking from the database where the imbalance is largest.
    working, reserve = _rebalance(working, reserve, frame, per_set)

    smoke_by_difficulty = largest_remainder(
        SMOKE_SIZE,
        {d: sum(1 for t in working if frame[t]["difficulty"] == d) for d in DIFFICULTIES},
    )
    smoke: list[str] = []
    for difficulty in DIFFICULTIES:
        candidates = sorted(t for t in working if frame[t]["difficulty"] == difficulty)
        rng.shuffle(candidates)
        smoke.extend(candidates[: smoke_by_difficulty[difficulty]])

    return {
        "working": sorted(working),
        "reserve": sorted(reserve),
        "smoke": sorted(smoke),
    }


def _rebalance(
    working: list[str],
    reserve: list[str],
    frame: dict[str, dict[str, str]],
    per_set: dict[str, int],
) -> tuple[list[str], list[str]]:
    """Move tasks between the sets until each holds exactly its difficulty allocation.

    Each move takes a task from the database where the two sets are furthest apart, so
    evening up the difficulty counts does not quietly skew the database spread. Fully
    deterministic: no generator is consumed here.
    """
    working, reserve = list(working), list(reserve)
    for difficulty in DIFFICULTIES:
        target = per_set[difficulty]
        while True:
            held = [t for t in working if frame[t]["difficulty"] == difficulty]
            if len(held) == target:
                break
            if len(held) > target:
                source, sink = working, reserve
            else:
                source, sink = reserve, working
            in_source = [t for t in source if frame[t]["difficulty"] == difficulty]
            in_sink = [t for t in sink if frame[t]["difficulty"] == difficulty]
            surplus: dict[str, int] = defaultdict(int)
            for task_id in in_source:
                surplus[frame[task_id]["db_id"]] += 1
            for task_id in in_sink:
                surplus[frame[task_id]["db_id"]] -= 1
            db_id = max(sorted(surplus), key=lambda d: surplus[d])
            chosen = min(t for t in in_source if frame[t]["db_id"] == db_id)
            source.remove(chosen)
            sink.append(chosen)
    return working, reserve


def marginals(task_ids: list[str], frame: dict[str, dict[str, str]], key: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for task_id in task_ids:
        counts[frame[task_id][key]] += 1
    return dict(sorted(counts.items()))


def describe(splits: dict[str, list[str]], frame: dict[str, dict[str, str]]) -> dict[str, Any]:
    return {
        name: {
            "count": len(ids),
            "difficulty": marginals(ids, frame, "difficulty"),
            "db_id": marginals(ids, frame, "db_id"),
        }
        for name, ids in splits.items()
    }
