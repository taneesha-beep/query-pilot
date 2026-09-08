"""Measure what the frame's reference queries actually return, so the caps can be derived.

`src/query_pilot/sandbox.py` sets a row cap, a byte cap and a statement timeout. All three
are policy choices, and this project's rule for a policy choice is that it says what it was
derived from. The derivation is the same for each: **a cap must not be able to decide a
legitimate answer.** So the largest, heaviest and slowest thing the substrate legitimately
asks for is measured here, and each cap is set above it with headroom that is stated.

The measurement uses `sandbox.result_size` rather than a second implementation of the same
arithmetic, because a cap derived with different arithmetic from the one that enforces it
is a cap derived from something else.

Executed **without** caps and with a generous timeout, deliberately: the point is to find
out what the uncapped truth is. `docs/sandbox-caps.json` is committed so that
`tests/test_sandbox.py` can guard the derivation in CI, where there is no substrate.

Run: uv run python scripts/sandbox_caps.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from query_pilot.sandbox import database_path, result_size

REPO = Path(__file__).resolve().parent.parent
SPIDER = REPO / "data" / "spider"
FRAME = REPO / "splits" / "frame.json"
OUT = REPO / "docs" / "sandbox-caps.json"

TASK_ID = "dev-{:04d}"


def connect_read_only(db: Path) -> sqlite3.Connection:
    """The sandbox's own connection settings, minus the caps this script exists to derive."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    conn.execute("PRAGMA query_only = ON")
    return conn


def percentile(values: list[float], share: float) -> float:
    """Nearest-rank, on a list already sorted ascending."""
    if not values:
        return 0.0
    index = max(0, min(len(values) - 1, round(share * len(values)) - 1))
    return values[index]


def main() -> int:
    if not (SPIDER / "dev.json").exists():
        print("substrate not acquired; see docs/SUBSTRATE.md", file=sys.stderr)
        return 2

    dev = json.loads((SPIDER / "dev.json").read_text())
    frame = set(json.loads(FRAME.read_text())["tasks"])

    connections: dict[str, sqlite3.Connection] = {}
    measured: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(dev):
            task_id = TASK_ID.format(index)
            if task_id not in frame:
                continue
            db_id = item["db_id"]
            if db_id not in connections:
                connections[db_id] = connect_read_only(database_path(SPIDER / "database", db_id))
            started = time.perf_counter()
            rows = connections[db_id].execute(item["query"]).fetchall()
            elapsed = time.perf_counter() - started
            measured.append(
                {
                    "task_id": task_id,
                    "db_id": db_id,
                    "rows": len(rows),
                    "bytes": result_size(rows),
                    "seconds": elapsed,
                }
            )
    finally:
        for conn in connections.values():
            conn.close()

    if len(measured) != len(frame):
        print(f"executed {len(measured)} of {len(frame)} frame tasks", file=sys.stderr)
        return 1

    by_rows = sorted(measured, key=lambda m: m["rows"])
    by_bytes = sorted(measured, key=lambda m: m["bytes"])
    by_time = sorted(measured, key=lambda m: m["seconds"])
    rows = [float(m["rows"]) for m in by_rows]
    sizes = [float(m["bytes"]) for m in by_bytes]
    times = [m["seconds"] for m in by_time]

    report = {
        "note": (
            "Uncapped execution of every reference query in splits/frame.json. The caps in "
            "src/query_pilot/sandbox.py are set above the maxima here, so that no reference "
            "result in this frame is decided by a cap."
        ),
        "frame_size": len(measured),
        "rows": {
            "max": by_rows[-1]["rows"],
            "max_task": by_rows[-1]["task_id"],
            "max_db": by_rows[-1]["db_id"],
            "p99": int(percentile(rows, 0.99)),
            "p95": int(percentile(rows, 0.95)),
            "median": int(percentile(rows, 0.50)),
        },
        "bytes": {
            "max": by_bytes[-1]["bytes"],
            "max_task": by_bytes[-1]["task_id"],
            "max_db": by_bytes[-1]["db_id"],
            "p99": int(percentile(sizes, 0.99)),
            "p95": int(percentile(sizes, 0.95)),
            "median": int(percentile(sizes, 0.50)),
            "definition": (
                "sandbox.encoded_size: UTF-8 length for TEXT, length for BLOB, 8 for "
                "INTEGER and REAL, 0 for NULL"
            ),
        },
        "seconds": {
            "max": round(by_time[-1]["seconds"], 6),
            "max_task": by_time[-1]["task_id"],
            "max_db": by_time[-1]["db_id"],
            "p99": round(percentile(times, 0.99), 6),
            "p95": round(percentile(times, 0.95), 6),
            "median": round(percentile(times, 0.50), 6),
            "total": round(sum(times), 6),
        },
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n")

    print(f"frame: {len(measured)}")
    print(
        f"rows    max {report['rows']['max']:>8} ({report['rows']['max_task']}, "
        f"{report['rows']['max_db']})  p99 {report['rows']['p99']}  "
        f"median {report['rows']['median']}"
    )
    print(
        f"bytes   max {report['bytes']['max']:>8} ({report['bytes']['max_task']})  "
        f"p99 {report['bytes']['p99']}  median {report['bytes']['median']}"
    )
    print(
        f"seconds max {report['seconds']['max']:>8.4f} ({report['seconds']['max_task']}, "
        f"{report['seconds']['max_db']})  p99 {report['seconds']['p99']:.4f}  "
        f"total {report['seconds']['total']:.2f}"
    )
    print(f"wrote {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
