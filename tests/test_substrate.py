"""Checks on the recorded substrate.

The first two run anywhere, including continuous integration, where no databases are
present: they read the committed fingerprint only. The third runs only when the substrate
has actually been acquired, and it is the one that catches a swapped substrate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FINGERPRINT = json.loads((REPO / "docs" / "substrate-fingerprint.json").read_text())
SPIDER = REPO / "data" / "spider"

needs_substrate = pytest.mark.skipif(
    not (SPIDER / "dev.json").exists(),
    reason="substrate not acquired; see docs/SUBSTRATE.md",
)


def test_fingerprint_is_internally_consistent() -> None:
    gold = FINGERPRINT["gold_execution"]
    assert gold["executed"] + gold["failed"] == FINGERPRINT["question_count"]
    assert gold["rate"] == round(gold["executed"] / FINGERPRINT["question_count"], 6)
    assert len(FINGERPRINT["executable_task_ids"]) == gold["executed"]
    assert len(set(FINGERPRINT["executable_task_ids"])) == gold["executed"]
    assert len(FINGERPRINT["databases"]) == FINGERPRINT["database_count"]
    assert FINGERPRINT["missing_databases"] == []


def test_fingerprint_records_the_substrate_this_project_was_built_on() -> None:
    assert FINGERPRINT["question_count"] == 1034
    assert FINGERPRINT["database_count"] == 20
    assert FINGERPRINT["archive"]["sha256"] == (
        "5ddff97bb1d421282c593e8d30ce0ce107270f4dd4a21d60eba4bf287d5956b1"
    )
    assert FINGERPRINT["dev_json_sha256"] == (
        "30d64a3fccde493226df79687aed9e4a1c0129525baf44f29c0573d914d758a4"
    )
    # The sampling frame is fixed: every task whose reference query executes.
    assert FINGERPRINT["gold_execution"]["failed"] == 0


@needs_substrate
def test_acquired_substrate_matches_the_fingerprint() -> None:
    dev_sha = hashlib.sha256((SPIDER / "dev.json").read_bytes()).hexdigest()
    assert dev_sha == FINGERPRINT["dev_json_sha256"]

    for recorded in FINGERPRINT["databases"]:
        db_id = recorded["db_id"]
        db = SPIDER / "database" / db_id / f"{db_id}.sqlite"
        assert db.exists(), f"{db_id} missing from the acquired substrate"
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
        try:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            assert tables == sorted(recorded["row_counts"]), f"{db_id} tables differ"
            for table, count in recorded["row_counts"].items():
                quoted = table.replace('"', '""')
                actual = conn.execute(f'SELECT count(*) FROM "{quoted}"').fetchone()[0]
                assert actual == count, f"{db_id}.{table}: {actual} rows, recorded {count}"
        finally:
            conn.close()
