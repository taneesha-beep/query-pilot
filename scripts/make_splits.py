"""Build the sampling frame and draw the working, reserve and smoke sets.

Writes splits/frame.json, splits/working.json, splits/reserve.json and splits/smoke.json.
Requires the substrate; see docs/SUBSTRATE.md.

Run: uv run python scripts/make_splits.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from query_pilot.difficulty import DIFFICULTIES, difficulty_of
from query_pilot.splits import SEED, build_splits, describe

REPO = Path(__file__).resolve().parent.parent
SPIDER = REPO / "data" / "spider"
SPLITS = REPO / "splits"
FINGERPRINT = REPO / "docs" / "substrate-fingerprint.json"


def main() -> int:
    fingerprint = json.loads(FINGERPRINT.read_text())
    dev = json.loads((SPIDER / "dev.json").read_text())
    dev_sha = hashlib.sha256((SPIDER / "dev.json").read_bytes()).hexdigest()
    if dev_sha != fingerprint["dev_json_sha256"]:
        raise SystemExit("dev.json does not match the recorded fingerprint; see docs/SUBSTRATE.md")

    # The frame is exactly the tasks whose reference query executes. Fixed in 0.2, never
    # revisited: re-deriving it after seeing results would invalidate every number on it.
    executable = set(fingerprint["executable_task_ids"])
    frame = {
        f"dev-{index:04d}": {
            "db_id": item["db_id"],
            "difficulty": difficulty_of(item["sql"]),
        }
        for index, item in enumerate(dev)
        if f"dev-{index:04d}" in executable
    }

    splits = build_splits(frame, seed=SEED)
    summary = describe(splits, frame)

    SPLITS.mkdir(exist_ok=True)
    (SPLITS / "frame.json").write_text(
        json.dumps(
            {
                "dev_json_sha256": dev_sha,
                "size": len(frame),
                "note": "tasks whose reference query executes; the frame the sets are drawn from",
                "tasks": frame,
            },
            indent=2,
        )
        + "\n"
    )
    for name, task_ids in splits.items():
        (SPLITS / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "seed": SEED,
                    "dev_json_sha256": dev_sha,
                    "frame_size": len(frame),
                    "count": len(task_ids),
                    "task_ids": task_ids,
                },
                indent=2,
            )
            + "\n"
        )

    print(f"frame: {len(frame)} tasks")
    for name in ("working", "reserve", "smoke"):
        counts = summary[name]["difficulty"]
        ordered = {d: counts.get(d, 0) for d in DIFFICULTIES}
        print(f"{name:>8}: {summary[name]['count']:>3} tasks  difficulty {ordered}")
    overlap = set(splits["working"]) & set(splits["reserve"])
    print(f"working/reserve overlap: {len(overlap)}")
    print(f"smoke inside working: {set(splits['smoke']) <= set(splits['working'])}")
    print()
    for name in ("working", "reserve"):
        print(f"{name} by db_id: {summary[name]['db_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
