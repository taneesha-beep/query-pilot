"""Measure what A1's tool results cost in a prompt, over every working-set database.

**This is where 3.1's rendering caps come from.** They are policy choices, like the
sandbox's caps and the equivalence rule's tolerance, and this project's rule is that a
chosen number says what it was derived from and has a test showing what it admits — so
this script produces `docs/a1-tool-sizes.json` and `tests/test_agents.py` reads it back.

**The condition being derived against is constraint 46**, which binds Phase 3 harder than
it bound Phase 2. Tokens are charged to the per-minute bucket when an answer arrives, so
one attempt must stay under the strong endpoint's TPM or the client's own wait ceiling
raises `AllPoolsExhausted` and the run ends itself with no provider having refused
anything. A0's attempts were a flat ~712 tokens. **A1's conversation grows every turn**, so
its last attempt carries every tool result of the trajectory, and that attempt is the one
that has to fit.

The arithmetic, with every input traceable:

    budget_chars = (tpm - max_output_tokens) x chars_per_prompt_token

`tpm` from `config/providers.toml` (the strong role's endpoint), `max_output_tokens` from
A0 — held equal so the two agents' answers have the same room — and
`chars_per_prompt_token` from `docs/a0-prompt-sizes.json`, which measured it against ten
real attempts rather than assuming it.

Two things are measured against that budget. The **exhaustive trajectory** —
`list_tables` once, `describe_table` on every table, `sample_rows` on every table at the
ceiling — which is more than any sensible trajectory does and is deliberately the worst
case, because a cap that only fits the median decides an outcome on the databases where it
does not. And the **largest single tool result**, plus the **longest value in the
substrate**, because `TOOL_RESULT_CHARS` and `VALUE_CHARS` have to sit above those on the
same reasoning 2.2 derived the sandbox's row and byte caps from.

    uv run python scripts/a1_tool_sizes.py [--out docs/a1-tool-sizes.json]

No API key, no quota, no network. It needs the substrate; see docs/SUBSTRATE.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from query_pilot.agents import MAX_OUTPUT_TOKENS
from query_pilot.agents.schema import quote, read_table_names
from query_pilot.agents.tools import (
    RESULT_ROWS,
    SAMPLE_ROWS_DEFAULT,
    SAMPLE_ROWS_MAX,
    TOOL_RESULT_CHARS,
    TOOL_SCHEMAS,
    VALUE_CHARS,
    describe_table,
    list_tables,
    sample_rows,
)
from query_pilot.sandbox import Sandbox, database_path
from query_pilot.tasks import load_tasks

REPO = Path(__file__).resolve().parent.parent
SPIDER = REPO / "data" / "spider"
WORKING = REPO / "splits" / "working.json"
PROVIDERS = REPO / "config" / "providers.toml"
A0_SIZES = REPO / "docs" / "a0-prompt-sizes.json"
DEFAULT_OUT = REPO / "docs" / "a1-tool-sizes.json"


def strong_tpm() -> tuple[str, int]:
    """The endpoint the `strong` role points at, and its tokens-per-minute.

    Read rather than written down, because no call site in this project names a model and
    a derivation that hard-coded 8,000 would go stale silently the day the role moved.
    """
    config = tomllib.loads(PROVIDERS.read_text())
    endpoint = config["roles"]["strong"]["endpoint"]
    return endpoint, int(config["endpoints"][endpoint]["tpm"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    options = parser.parse_args()

    if not (SPIDER / "dev.json").exists():
        print("substrate not acquired; see docs/SUBSTRATE.md", file=sys.stderr)
        return 2

    endpoint, tpm = strong_tpm()
    ratio = json.loads(A0_SIZES.read_text())["chars_per_prompt_token"]["value"]
    budget_chars = (tpm - MAX_OUTPUT_TOKENS) * ratio

    tasks = load_tasks(SPIDER, WORKING)
    db_ids = sorted({task.db_id for task in tasks})
    sandbox = Sandbox()

    databases = []
    longest_value = 0
    longest_value_at = None
    values_seen = 0
    values_over_cap = 0
    for db_id in db_ids:
        database = database_path(SPIDER / "database", db_id)
        names = read_table_names(sandbox, database, db_id)
        listing = len(list_tables(sandbox, database, {}).content)
        describes = [len(describe_table(sandbox, database, {"table": n}).content) for n in names]
        samples = {
            str(limit): [
                len(sample_rows(sandbox, database, {"table": n, "limit": limit}).content)
                for n in names
            ]
            for limit in (SAMPLE_ROWS_DEFAULT, SAMPLE_ROWS_MAX)
        }
        # VALUE_CHARS has to sit above the longest value in the substrate or it silently
        # decides what the model sees, which is the requirement 2.2 derived its caps
        # against. Every TEXT value in the working set is read to check it.
        for name in names:
            whole = sandbox.execute(database, f"SELECT * FROM {quote(name)}")
            for row in whole.rows:
                for value in row:
                    if not isinstance(value, str):
                        continue
                    values_seen += 1
                    length = len(" ".join(value.split()))
                    values_over_cap += length > VALUE_CHARS
                    if length > longest_value:
                        longest_value, longest_value_at = length, f"{db_id}.{name}"
        databases.append(
            {
                "db_id": db_id,
                "tables": len(names),
                "list_tables_chars": listing,
                "describe_table_chars_sum": sum(describes),
                "describe_table_chars_max": max(describes, default=0),
                "sample_rows_chars_sum": {k: sum(v) for k, v in samples.items()},
                "sample_rows_chars_max": {k: max(v, default=0) for k, v in samples.items()},
                # Every tool result an exhaustive trajectory on this database could carry:
                # the listing, every table described, every table sampled at the ceiling.
                # More than any sensible trajectory does, and deliberately so — a cap that
                # only fits the median decides an outcome on the databases where it does not.
                "exhaustive_chars": (listing + sum(describes) + sum(samples[str(SAMPLE_ROWS_MAX)])),
            }
        )

    worst = max(databases, key=lambda row: row["exhaustive_chars"])
    # The largest single tool result this substrate can produce under these caps. This is
    # the number TOOL_RESULT_CHARS has to sit above: a backstop that cuts a legitimate
    # result is a backstop that decides what the model sees.
    largest_result = max(
        max(row["describe_table_chars_max"], row["sample_rows_chars_max"][str(SAMPLE_ROWS_MAX)])
        for row in databases
    )

    report = {
        "split": "working",
        "tasks": len(tasks),
        "databases": len(db_ids),
        "budget": {
            "endpoint": endpoint,
            "tpm": tpm,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "chars_per_prompt_token": ratio,
            "chars_per_prompt_token_source": "docs/a0-prompt-sizes.json",
            "budget_chars": round(budget_chars, 1),
            "note": (
                "One attempt must stay under tpm at concurrency 1 or the client's own "
                "bucket ends the run as pools_exhausted with no provider having refused "
                "anything. A1's last attempt carries every tool result of the trajectory, "
                "so this is the budget every tool result in one trajectory shares."
            ),
        },
        "caps": {
            "RESULT_ROWS": RESULT_ROWS,
            "SAMPLE_ROWS_DEFAULT": SAMPLE_ROWS_DEFAULT,
            "SAMPLE_ROWS_MAX": SAMPLE_ROWS_MAX,
            "VALUE_CHARS": VALUE_CHARS,
        },
        "tools": [schema.name for schema in TOOL_SCHEMAS],
        "worst_database": {
            "db_id": worst["db_id"],
            "tables": worst["tables"],
            "exhaustive_chars": worst["exhaustive_chars"],
            "exhaustive_tokens_estimated": round(worst["exhaustive_chars"] / ratio),
            "share_of_budget": round(worst["exhaustive_chars"] / budget_chars, 4),
        },
        "largest_tool_result": {
            "chars": largest_result,
            "tokens_estimated": round(largest_result / ratio),
            "backstop": TOOL_RESULT_CHARS,
            "backstop_margin": round(TOOL_RESULT_CHARS / largest_result, 3),
            "note": (
                "The largest single tool result the working set produces under these caps. "
                "TOOL_RESULT_CHARS must sit above it, on the same reasoning 2.2 used for the "
                "sandbox's row and byte caps. If the backstop ever binds in a real run, "
                "RESULT_ROWS was the wrong shape rather than the backstop the wrong size."
            ),
        },
        "longest_value": {
            "chars": longest_value,
            "where": longest_value_at,
            "cap": VALUE_CHARS,
            "cap_margin": round(VALUE_CHARS / longest_value, 3) if longest_value else None,
            "text_values_seen": values_seen,
            "text_values_over_cap": values_over_cap,
            "note": (
                "VALUE_CHARS truncates nothing in this substrate. It is here for the "
                "pathology the row cap cannot bound — few rows holding very large values — "
                "which is the pair the sandbox's row and byte caps bound one layer down."
            ),
        },
        "databases_measured": databases,
        "why_this_file_exists": (
            "src/query_pilot/agents/tools.py chooses RESULT_ROWS, SAMPLE_ROWS_MAX and "
            "VALUE_CHARS, and src/query_pilot/agents/a1.py chooses a turn limit and a "
            "tool-call limit. All five are policy choices and this is what they were "
            "derived against. tests/test_agents.py reads this file so CI checks the "
            "arithmetic with no substrate and no keys, the same shape as "
            "docs/sandbox-caps.json."
        ),
    }
    options.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "databases_measured"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
