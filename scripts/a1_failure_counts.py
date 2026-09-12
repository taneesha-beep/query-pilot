"""4.4 — the mechanical counts beside A1's hand-read failures. **Spends nothing.**

`docs/FAILURES.md` categorises A1's 34 non-solves by hand. Three things beside those categories
are not a matter of reading and are computed here instead, so that each has an artifact and a
command rather than a sentence:

1. **A solving query already in hand.** For every non-solve in the census
   (`docs/failure-sample-a1.json`), every `execute_sql` the trajectory actually ran is executed
   again, read-only, through the same sandbox, and compared against the reference with the same
   equivalence rule A1 was scored by. **No verdict moves** — the task's verdict is the ledger's
   and this only asks whether a statement the model had already run would have been a solve.
2. **Repeated identical tool calls.** An executed tool call whose name and arguments equal an
   earlier executed call in the same trajectory, over every trajectory of the run. Defined here
   before it was computed, 2026-09-12.
3. **Tool calls counted two ways.** Over the 150 trajectories that stand (a task's last bracket,
   the reading `agents/metrics.py` and `results/a1-trajectory-metrics.json` use) and over every
   bracket in the transcript files, including trajectories cut off by a session boundary and
   retried — because `docs/RESULTS.md` quoted the second as though it were the first.

And one set of **verification queries**: for the eight of the ten tasks A1 lost and A0 won
whose reference is claimed to return wrong data, the read-only query that shows what the stored
data actually answers, beside what the reference returns and whether A0 solved it. It is what
makes "the reference returns demonstrably wrong data" a claim with a command behind it. The
other two of the ten are a question with two readings and a sort order, and have no such query.

Needs the substrate (`data/spider`) and the run directory (`runs/`), both gitignored, exactly
as `scripts/a1_metrics.py` does. Never reads `splits/reserve.json`.

    uv run python scripts/a1_failure_counts.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from query_pilot.equivalence import compare, orders_rows
from query_pilot.run import read_rows
from query_pilot.sandbox import Sandbox, database_path

REPO = Path(__file__).resolve().parent.parent
RUN_ID = "20260910-024454-1f69bc"
A0_RUN_ID = "20260908-133316-faecd5"
RUN = REPO / "runs" / RUN_ID
CENSUS = REPO / "docs" / "failure-sample-a1.json"
DATABASES = REPO / "data" / "spider" / "database"
OUT = REPO / "docs" / "a1-failure-counts.json"

#: What the stored data answers, for each task A1 lost and A0 won where the reference is claimed
#: to return wrong data. Each strips the padding the reference trips on, or looks at the value it
#: compares against, and nothing else.
VERIFICATION = {
    "dev-0186": "SELECT AirportCode, AirportName FROM airports WHERE trim(City) = 'Anthony'",
    "dev-0207": (
        "SELECT count(*) FROM flights f JOIN airports a ON trim(f.SourceAirport) = a.AirportCode "
        "WHERE trim(a.City) = 'Aberdeen'"
    ),
    "dev-0227": (
        "SELECT T1.AirportCode FROM airports T1 JOIN flights T2 ON T1.AirportCode = "
        "trim(T2.DestAirport) OR T1.AirportCode = trim(T2.SourceAirport) GROUP BY "
        "T1.AirportCode ORDER BY count(*) LIMIT 1"
    ),
    "dev-0238": (
        "SELECT T1.Airline FROM airlines T1 JOIN flights T2 ON T1.uid = T2.Airline WHERE "
        "trim(T2.SourceAirport) = 'APG' INTERSECT SELECT T1.Airline FROM airlines T1 JOIN "
        "flights T2 ON T1.uid = T2.Airline WHERE trim(T2.SourceAirport) = 'CVO'"
    ),
    "dev-0248": "SELECT FlightNo FROM flights WHERE trim(SourceAirport) = 'APG'",
    "dev-0254": (
        "SELECT T1.FlightNo FROM flights T1 JOIN airports T2 ON trim(T1.DestAirport) = "
        "T2.AirportCode WHERE trim(T2.City) = 'Aberdeen'"
    ),
    "dev-0256": (
        "SELECT count(*) FROM flights T1 JOIN airports T2 ON trim(T1.DestAirport) = "
        "T2.AirportCode WHERE trim(T2.City) IN ('Aberdeen', 'Abilene')"
    ),
    "dev-0388": "SELECT Name, Hometown FROM teacher WHERE Hometown LIKE 'little lever%'",
}


def last_rows(ledger: Path) -> dict[str, dict]:
    """A task's last ledger row stands."""
    return {str(r["task_id"]): r for r in read_rows(ledger) if r.get("kind") == "task"}


def brackets(path: Path) -> list[list[dict]]:
    events = sorted(
        (json.loads(line) for line in path.read_text().splitlines() if line.strip()),
        key=lambda event: event["seq"],
    )
    out: list[list[dict]] = []
    for event in events:
        if event["kind"] == "start":
            out.append([])
        if out:
            out[-1].append(event)
    return out


def executed_calls(events: list[dict]) -> list[tuple[int, dict]]:
    """Every tool call that has a result, with its turn, in order."""
    ran = {e["call_id"] for e in events if e["kind"] == "tool_result"}
    return [
        (int(e["turn"]), call)
        for e in events
        if e["kind"] == "message" and e["role"] == "assistant"
        for call in e.get("tool_calls") or ()
        if call["id"] in ran
    ]


def rows_of(result) -> list[list]:
    return [list(row) for row in result.rows] if result.ok else []


def main() -> int:
    box = Sandbox()
    rows = last_rows(RUN / "ledger.jsonl")
    census = [d["task_id"] for d in json.loads(CENSUS.read_text())["drawn"]]

    solving: list[dict] = []
    for task_id in census:
        detail = rows[task_id]["detail"]
        database = database_path(DATABASES, detail["db_id"])
        events = brackets(RUN / detail["transcript"])[-1]
        reference = box.execute(database, detail["reference_sql"])
        turns = []
        for turn, call in executed_calls(events):
            if call["name"] != "execute_sql":
                continue
            candidate = box.execute(database, call["arguments"]["sql"])
            verdict = compare(
                reference.rows if reference.ok else None,
                candidate.rows if candidate.ok else None,
                ordered=orders_rows(detail["reference_sql"]),
                reference_error=reference.error,
                candidate_error=candidate.error,
                reference_truncated=reference.truncated,
                candidate_truncated=candidate.truncated,
            )
            if verdict.solved:
                turns.append(turn)
        if turns:
            solving.append(
                {
                    "task_id": task_id,
                    "db_id": detail["db_id"],
                    "reason": detail["reason"],
                    "termination": detail["termination"],
                    "solving_execute_sql_at_turns": turns,
                }
            )

    repeats: list[dict] = []
    standing: Counter[str] = Counter()
    every: Counter[str] = Counter()
    bracket_count = 0
    for path in sorted((RUN / "transcripts").glob("*.jsonl")):
        found = brackets(path)
        bracket_count += len(found)
        for index, events in enumerate(found):
            calls = executed_calls(events)
            for _, call in calls:
                every[call["name"]] += 1
                if index == len(found) - 1:
                    standing[call["name"]] += 1
            if index != len(found) - 1:
                continue
            seen: set[tuple[str, str]] = set()
            repeated = 0
            for _, call in calls:
                key = (call["name"], json.dumps(call.get("arguments") or {}, sort_keys=True))
                repeated += key in seen
                seen.add(key)
            if repeated:
                detail = rows[path.stem]["detail"]
                repeats.append(
                    {
                        "task_id": path.stem,
                        "solved": bool(detail.get("solved")),
                        "termination": detail["termination"],
                        "repeated_calls": repeated,
                    }
                )

    a0 = last_rows(REPO / "runs" / A0_RUN_ID / "ledger.jsonl")
    verification = []
    for task_id, sql in VERIFICATION.items():
        detail = rows[task_id]["detail"]
        database = database_path(DATABASES, detail["db_id"])
        verification.append(
            {
                "task_id": task_id,
                "db_id": detail["db_id"],
                "reference_returns": rows_of(box.execute(database, detail["reference_sql"])),
                "verification_sql": sql,
                "stored_data_answers": rows_of(box.execute(database, sql)),
                "a0_solved": bool(a0[task_id]["detail"].get("solved")),
                "a0_sql": a0[task_id]["detail"]["sql"],
            }
        )

    solved_total = sum(1 for r in rows.values() if r["detail"].get("solved"))
    document = {
        "run_id": RUN_ID,
        "ledger": f"runs/{RUN_ID}/ledger.jsonl",
        "census": "docs/failure-sample-a1.json",
        "definitions": {
            "solving_query_in_hand": (
                "A census non-solve in whose standing trajectory some execute_sql that ran, "
                "re-executed read-only through the same sandbox, compares as solved against the "
                "reference under the same equivalence rule. No verdict moves."
            ),
            "repeated_identical_call": (
                "An executed tool call whose name and arguments equal an earlier executed call "
                "in the same standing trajectory."
            ),
            "standing": "A task's last start-to-end bracket, the one metrics.py reads.",
        },
        "solving_query_in_hand": {
            "count": len(solving),
            "of": len(census),
            "tasks": solving,
        },
        "repeated_identical_calls": {
            "calls": sum(r["repeated_calls"] for r in repeats),
            "of_executed_calls": sum(standing.values()),
            "trajectories": len(repeats),
            "of_trajectories": len(rows),
            "among_non_solves": sum(1 for r in repeats if not r["solved"]),
            "of_non_solves": len(census),
            "among_solved": sum(1 for r in repeats if r["solved"]),
            "of_solved": solved_total,
            "tasks": repeats,
        },
        "tool_calls": {
            "standing_trajectories": len(rows),
            "standing": dict(sorted(standing.items())),
            "standing_total": sum(standing.values()),
            "brackets": bracket_count,
            "every_bracket": dict(sorted(every.items())),
            "every_bracket_total": sum(every.values()),
        },
        "reference_verification": verification,
    }
    OUT.write_text(json.dumps(document, indent=2) + "\n")
    print(
        json.dumps({k: v for k, v in document.items() if k != "reference_verification"}, indent=2)
    )
    for item in verification:
        print(
            item["task_id"],
            "reference",
            item["reference_returns"][:3],
            "| data",
            item["stored_data_answers"][:3],
            "| A0 solved",
            item["a0_solved"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
