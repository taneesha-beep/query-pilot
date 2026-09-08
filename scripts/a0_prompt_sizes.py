"""How large A0's prompt gets over a split, written where a test can read it.

`config/runs/working-set.toml` declares a concurrency, and that number is only safe if
`concurrency x tokens-per-attempt` stays under the endpoint's per-minute token ceiling —
above it, the burst of in-flight requests puts the token bucket into a deficit deeper than
`wait_ceiling_s` of refill, the client raises `AllPoolsExhausted`, and the run ends as
`pools_exhausted` without a provider having refused anything.

So the declaration rests on a measured figure: **the most tokens one attempt can cost.**
This script measures it, the way `scripts/sandbox_caps.py` measures the sandbox's caps, and
writes it to `docs/a0-prompt-sizes.json` — which is committed, so CI guards the arithmetic
on a machine with no substrate and no keys.

**Characters are exact; tokens are not, and the file says so.** The provider counts tokens
and this project does not tokenise. The ratio is measured against real attempts: a ledger's
`prompt_tokens` are known, this script rebuilds those exact prompts, and the
characters-per-token ratio falls out. That makes the estimate specific to this prompt, this
schema renderer and this model, rather than a rule of thumb.

    uv run python scripts/a0_prompt_sizes.py [--split working] [--ledger runs/<id>/ledger.jsonl]
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
from pathlib import Path

from query_pilot.agents import MAX_OUTPUT_TOKENS, build_prompt, read_schema
from query_pilot.run import read_rows
from query_pilot.sandbox import CopyScope, Sandbox, SubstrateCopies
from query_pilot.tasks import load_tasks

REPO = Path(__file__).resolve().parent.parent
SPIDER = REPO / "data" / "spider"
OUT = REPO / "docs" / "a0-prompt-sizes.json"

#: 2.3's acceptance run: ten real attempts on this prompt, whose token counts the provider
#: reported. Gitignored like every ledger, so this is where it came from rather than
#: something a fresh clone can re-derive.
DEFAULT_LEDGER = REPO / "runs" / "20260908-130225-514f3e" / "ledger.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", default="working")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    options = parser.parse_args()

    if not (SPIDER / "dev.json").exists():
        raise SystemExit("substrate not acquired; see docs/SUBSTRATE.md")
    if not options.ledger.exists():
        raise SystemExit(f"no ledger at {options.ledger}; the ratio is measured against one")

    tasks = load_tasks(SPIDER, REPO / "splits" / f"{options.split}.json")
    sandbox = Sandbox()
    copies = SubstrateCopies(SPIDER / "database", scope=CopyScope.RUN)
    schemas: dict[str, object] = {}
    chars: dict[str, int] = {}
    try:
        for task in tasks:
            with copies.for_task(task.db_id, task_id=task.task_id) as database:
                if task.db_id not in schemas:
                    schemas[task.db_id] = read_schema(sandbox, database, task.db_id)
            chars[task.task_id] = sum(
                len(message.content) for message in build_prompt(schemas[task.db_id], task.question)
            )
    finally:
        copies.close()

    attempts = [
        row
        for row in read_rows(options.ledger)
        if row.get("kind") == "attempt" and row.get("prompt_tokens") and row["task_id"] in chars
    ]
    if not attempts:
        raise SystemExit(f"{options.ledger} holds no answered attempt for a task in this split")

    ratios = [chars[row["task_id"]] / row["prompt_tokens"] for row in attempts]
    ratio = sum(chars[row["task_id"]] for row in attempts) / sum(
        row["prompt_tokens"] for row in attempts
    )
    largest = max(chars.values())
    estimated = {task_id: round(count / ratio) for task_id, count in chars.items()}
    provider = attempts[0].get("provider")
    model = attempts[0].get("model")

    # How many consecutive tasks the split's order draws from one database. It matters
    # because in-flight requests are taken in that order: `concurrency` tasks in flight are
    # very often `concurrency` tasks on the *same* schema, and so the same prompt size.
    longest_run = run = 1
    for before, after in itertools.pairwise(tasks):
        run = run + 1 if before.db_id == after.db_id else 1
        longest_run = max(longest_run, run)

    largest_db = max(schemas, key=lambda db: max(chars[t.task_id] for t in tasks if t.db_id == db))
    largest_db_tokens = round(max(chars[t.task_id] for t in tasks if t.db_id == largest_db) / ratio)

    document = {
        "split": options.split,
        "tasks": len(tasks),
        "databases": len(schemas),
        "longest_same_database_run": longest_run,
        "largest_database": {
            "db_id": largest_db,
            "prompt_tokens_estimated": largest_db_tokens,
            "note": (
                "Tasks are taken in split order and that order groups by database, so a "
                "burst of in-flight requests is usually a burst on one schema. The size "
                "that matters for a concurrency decision is this one, not the median."
            ),
        },
        "measured_from": {
            "ledger": str(options.ledger.relative_to(REPO)),
            "run_id": attempts[0].get("run_id"),
            "attempts": len(attempts),
            "provider": provider,
            "model": model,
            "date": str(attempts[0].get("recorded_at"))[:10],
        },
        "chars_per_prompt_token": {
            "value": round(ratio, 3),
            "min": round(min(ratios), 3),
            "max": round(max(ratios), 3),
            "median": round(statistics.median(ratios), 3),
            "note": (
                "Measured, not assumed: the prompts of the attempts above were rebuilt "
                "character for character and divided by the token counts the provider "
                "reported for them."
            ),
        },
        "prompt_chars": {
            "min": min(chars.values()),
            "median": int(statistics.median(chars.values())),
            "max": largest,
            "note": "Exact. Everything below is these divided by the ratio above.",
        },
        "prompt_tokens_estimated": {
            "min": min(estimated.values()),
            "median": int(statistics.median(estimated.values())),
            "max": max(estimated.values()),
            "sum": sum(estimated.values()),
        },
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "worst_case_attempt_tokens": max(estimated.values()) + MAX_OUTPUT_TOKENS,
        "why_this_file_exists": (
            "config/runs/working-set.toml declares a concurrency, and it is only safe while "
            "concurrency x worst_case_attempt_tokens stays under the strong endpoint's tpm in "
            "config/providers.toml. Above that the in-flight burst puts the per-minute token "
            "bucket into a deficit deeper than wait_ceiling_s of refill, the client raises "
            "AllPoolsExhausted, and the run ends as pools_exhausted with no provider having "
            "refused anything. tests/test_run_budget.py holds the arithmetic."
        ),
    }
    OUT.write_text(json.dumps(document, indent=2) + "\n")
    print(json.dumps({k: v for k, v in document.items() if k != "why_this_file_exists"}, indent=2))
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
