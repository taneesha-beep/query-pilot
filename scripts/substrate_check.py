"""Fingerprint the Spider dev substrate and run every gold query against its database.

Writes docs/substrate-fingerprint.json. Two jobs:

1. **Fingerprint.** Database count, tables per database and row counts, so that a later
   substrate swap is detectable even if the recorded checksum line has gone stale.
2. **Gold execution.** Every dev reference query is executed against its own database.
   A task whose reference query raises cannot be scored by execution, so this run fixes
   the frame that the working set and the reserve set are drawn from.

"Executes" means "does not raise". An empty result is a legitimate reference result and
counts as executing, because the equivalence rule scores empty against empty as a solve.

Run: uv run python scripts/substrate_check.py
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
SPIDER = REPO / "data" / "spider"
ARCHIVE = REPO / "data" / "raw" / "spider.zip"
OUT = REPO / "docs" / "substrate-fingerprint.json"

# A task ID is the zero-based position of the question in dev.json. That is only stable
# because dev.json is pinned by the checksum recorded in docs/SUBSTRATE.md.
TASK_ID = "dev-{:04d}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def connect_read_only(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    # Several Spider databases hold bytes that are not valid UTF-8. Decoding them is the
    # client's problem, not the query's, so tolerate it rather than record a query failure
    # that is really a codec failure. The Spider evaluation scripts do the same.
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    return conn


def fingerprint_database(db_id: str) -> dict[str, Any]:
    db = SPIDER / "database" / db_id / f"{db_id}.sqlite"
    if not db.exists():
        return {"db_id": db_id, "present": False}
    conn = connect_read_only(db)
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        rows = {}
        for name in names:
            quoted = name.replace('"', '""')
            rows[name] = conn.execute(f'SELECT count(*) FROM "{quoted}"').fetchone()[0]
    finally:
        conn.close()
    return {
        "db_id": db_id,
        "present": True,
        "bytes": db.stat().st_size,
        "table_count": len(names),
        "row_counts": rows,
        "total_rows": sum(rows.values()),
    }


def main() -> int:
    dev = json.loads((SPIDER / "dev.json").read_text())
    db_ids = sorted({item["db_id"] for item in dev})

    databases = [fingerprint_database(db_id) for db_id in db_ids]
    missing = [d["db_id"] for d in databases if not d["present"]]

    connections = {
        db_id: connect_read_only(SPIDER / "database" / db_id / f"{db_id}.sqlite")
        for db_id in db_ids
        if db_id not in missing
    }

    executed: list[str] = []
    failed: list[dict[str, str]] = []
    empty: list[str] = []
    try:
        for index, item in enumerate(dev):
            task_id = TASK_ID.format(index)
            conn = connections.get(item["db_id"])
            if conn is None:
                failed.append(
                    {"task_id": task_id, "db_id": item["db_id"], "error": "database missing"}
                )
                continue
            try:
                rows = conn.execute(item["query"]).fetchall()
            except Exception as exc:  # every failure class is recorded, not raised
                failed.append(
                    {
                        "task_id": task_id,
                        "db_id": item["db_id"],
                        "error": f"{type(exc).__name__}: {exc}",
                        "query": item["query"],
                    }
                )
                continue
            executed.append(task_id)
            if not rows:
                empty.append(task_id)
    finally:
        for conn in connections.values():
            conn.close()

    report = {
        "archive": {
            "path": str(ARCHIVE.relative_to(REPO)),
            "sha256": sha256(ARCHIVE) if ARCHIVE.exists() else None,
            "bytes": ARCHIVE.stat().st_size if ARCHIVE.exists() else None,
        },
        "dev_json_sha256": sha256(SPIDER / "dev.json"),
        "tables_json_sha256": sha256(SPIDER / "tables.json"),
        "question_count": len(dev),
        "database_count": len(db_ids),
        "missing_databases": missing,
        "databases": databases,
        "gold_execution": {
            "executed": len(executed),
            "failed": len(failed),
            "rate": round(len(executed) / len(dev), 6),
            "empty_results": len(empty),
            "failures": failed,
        },
        "executable_task_ids": executed,
    }
    OUT.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n")

    print(f"databases: {len(db_ids)}  missing: {len(missing)}")
    print(f"questions: {len(dev)}")
    print(f"gold executed: {len(executed)}  failed: {len(failed)}")
    print(f"gold execution rate: {len(executed) / len(dev):.4%}")
    print(f"gold queries returning no rows: {len(empty)}")
    for f in failed:
        print(f"  FAIL {f['task_id']} [{f['db_id']}] {f['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
