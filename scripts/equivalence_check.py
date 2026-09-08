"""Apply the equivalence rule to the gold queries against themselves, over the whole frame.

The roadmap's acceptance for 2.1 is that this reports 100%. On its own that is close to a
tautology — the same query run twice against the same database returns the same rows — so
this script does two more things that are not:

**It runs the rule against real data.** Spider results hold NULLs, mixed types in one
column, reals that came out of an aggregate, empty results, and the `wta_1` bytes that are
not valid UTF-8. Any of those can take down a comparison that was only ever tried on
hand-built fixtures, and a sort key over a column holding a NULL, an integer and a string
is the specific thing that would.

**It permutes the rows.** Every unordered comparison is re-run with the candidate's rows
shuffled and must still solve; every ordered one is re-run shuffled and must now fail. That
is the only part of this check with teeth, and it tests `orders_rows` against 1,034 real
reference queries rather than against the dozen in the unit tests.

Each query is executed **twice**, into two separate result lists, so that a query whose row
order is not stable across executions shows up here rather than in 2.4.

No cap is applied to anything: 2.2's row and byte caps do not exist yet, and the rule's
`truncated` path is exercised by the unit tests instead.

Writes docs/equivalence-check.json.

Run: uv run python scripts/equivalence_check.py
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path
from typing import Any

from query_pilot.equivalence import compare, orders_rows

REPO = Path(__file__).resolve().parent.parent
SPIDER = REPO / "data" / "spider"
FRAME = REPO / "splits" / "frame.json"
OUT = REPO / "docs" / "equivalence-check.json"

#: Committed so the permutation is the same one every time this is re-run.
SHUFFLE_SEED = 20260908

TASK_ID = "dev-{:04d}"


def connect_read_only(db: Path) -> sqlite3.Connection:
    """The same connection 0.2 opened, for the same reasons.

    Read-only because nothing here may alter the substrate, and the text factory replaces
    undecodable bytes because several Spider databases hold some. **That tolerance belongs
    to the connection and not to the equivalence rule**: both sides of every comparison are
    decoded the same way, so both carry U+FFFD and compare equal, and a decode fault is
    never read as a wrong answer. Strict decoding would report 99.81% at 0.2 rather than
    100.00%, and it would be reporting on the stored bytes rather than on the query.

    2.2 replaces this with the sandbox executor, which adds a statement timeout, a row cap,
    a byte cap and a per-task copy. Those four are what 2.2 is for; these two lines are the
    part it inherits.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    return conn


def main() -> int:
    if not (SPIDER / "dev.json").exists():
        print("substrate not acquired; see docs/SUBSTRATE.md", file=sys.stderr)
        return 2

    dev = json.loads((SPIDER / "dev.json").read_text())
    frame = set(json.loads(FRAME.read_text())["tasks"])
    rng = random.Random(SHUFFLE_SEED)

    connections: dict[str, sqlite3.Connection] = {}
    solved = 0
    failures: list[dict[str, Any]] = []
    ordered_count = 0
    permuted_unordered = permuted_unordered_solved = 0
    permuted_ordered = permuted_ordered_rejected = 0
    empty_results = 0

    try:
        for index, item in enumerate(dev):
            task_id = TASK_ID.format(index)
            if task_id not in frame:
                continue
            db_id = item["db_id"]
            if db_id not in connections:
                connections[db_id] = connect_read_only(
                    SPIDER / "database" / db_id / f"{db_id}.sqlite"
                )
            conn = connections[db_id]
            sql = item["query"]
            ordered = orders_rows(sql)
            ordered_count += ordered

            try:
                reference = conn.execute(sql).fetchall()
                candidate = conn.execute(sql).fetchall()
            except Exception as exc:  # recorded, never raised: the frame says these run
                failures.append(
                    {"task_id": task_id, "db_id": db_id, "reason": "execution", "detail": str(exc)}
                )
                continue

            if not reference:
                empty_results += 1

            verdict = compare(reference, candidate, ordered=ordered)
            if verdict.solved:
                solved += 1
            else:
                failures.append(
                    {
                        "task_id": task_id,
                        "db_id": db_id,
                        "reason": verdict.reason,
                        "detail": verdict.detail,
                        "query": sql,
                        "ordered": ordered,
                    }
                )

            # -- the permutation control -------------------------------------------------
            shuffled = list(candidate)
            rng.shuffle(shuffled)
            if shuffled == list(candidate):
                continue  # nothing was actually permuted; it proves nothing either way
            permuted = compare(reference, shuffled, ordered=ordered)
            if ordered:
                permuted_ordered += 1
                permuted_ordered_rejected += not permuted.solved
                if permuted.solved:
                    failures.append(
                        {
                            "task_id": task_id,
                            "db_id": db_id,
                            "reason": "order_not_enforced",
                            "detail": "reference has a top-level ORDER BY, "
                            "but a permuted result still solved",
                            "query": sql,
                            "ordered": True,
                        }
                    )
            else:
                permuted_unordered += 1
                permuted_unordered_solved += permuted.solved
                if not permuted.solved:
                    failures.append(
                        {
                            "task_id": task_id,
                            "db_id": db_id,
                            "reason": "order_wrongly_enforced",
                            "detail": f"reference fixes no order, "
                            f"but a permuted result failed: {permuted.detail}",
                            "query": sql,
                            "ordered": False,
                        }
                    )
    finally:
        for conn in connections.values():
            conn.close()

    total = len(frame)
    report = {
        "frame_size": len(frame),
        "shuffle_seed": SHUFFLE_SEED,
        "tolerance": {"absolute": 1e-9, "relative": 1e-9},
        "self_comparison": {
            "solved": solved,
            "rate": round(solved / total, 6),
            "empty_results": empty_results,
            "empty_share": round(empty_results / total, 6),
        },
        "row_order": {
            "reference_orders_rows": ordered_count,
            "reference_fixes_no_order": total - ordered_count,
        },
        "permutation_control": {
            "unordered_permuted": permuted_unordered,
            "unordered_still_solved": permuted_unordered_solved,
            "ordered_permuted": permuted_ordered,
            "ordered_now_rejected": permuted_ordered_rejected,
        },
        "failures": failures,
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n")

    print(f"frame: {total}")
    print(f"gold against itself: {solved}/{total} = {solved / total:.4%}")
    print(f"  of which empty results: {empty_results} ({empty_results / total:.4%})")
    print(f"reference fixes an order: {ordered_count}; fixes none: {total - ordered_count}")
    print(
        f"permuted and still solved (unordered): {permuted_unordered_solved}/{permuted_unordered}"
    )
    print(f"permuted and now rejected (ordered): {permuted_ordered_rejected}/{permuted_ordered}")
    for f in failures[:20]:
        print(f"  FAIL {f['task_id']} [{f['db_id']}] {f['reason']}: {f['detail'][:120]}")
    if len(failures) > 20:
        print(f"  ... and {len(failures) - 20} more, in {OUT.relative_to(REPO)}")
    return 0 if solved == total and not failures else 1


if __name__ == "__main__":
    sys.exit(main())
