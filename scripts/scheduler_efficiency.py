"""6.1, scheduler efficiency, written to `results/scheduler-efficiency.json`. **Spends nothing.**

Reads three gitignored ledgers, the quota-wall log and two runs' transcripts, and hands them to
`src/query_pilot/run/throughput.py`, whose reading was fixed in `docs/PERFORMANCE.md` before any
ratio but A0's glimpse existed (`d027fc7`).

    uv run python scripts/scheduler_efficiency.py

The transcripts supply one thing the ledger lacks: how long each turn's tool calls took, where
the tool records it (`execute_sql`, `sample_rows`). They are joined to attempt rows by task and
by time — an attempt belongs to the bracket it was answered inside — and by turn.
``tests/test_throughput.py`` recomputes the written file from the inputs it carries alone.
Nothing here opens a split file, so the reserve set is not read.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

from query_pilot.agents.transcript import read_trajectories
from query_pilot.client.config import ClientConfig, Limits
from query_pilot.run.ledger import read_rows
from query_pilot.run.throughput import (
    SENSITIVITY_S,
    TOLERANCE_S,
    document,
    dumps,
    sessions_from_ledger,
)

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "results" / "scheduler-efficiency.json"
WALLS = "runs/quota-walls.jsonl"
HEADLINE = "20260910-024454-1f69bc"
RUNS = (
    ("A1", "20260910-024454-1f69bc", True),
    ("A0", "20260908-133316-faecd5", False),
    ("A2-cheap", "20260912-055938-9712c8", True),
)


def limits_for(config: ClientConfig):
    def lookup(model: str) -> Limits:
        found = [e.limits for e in config.endpoints.values() if e.model == model]
        if len(found) != 1:
            raise SystemExit(f"{len(found)} endpoints serve {model}; expected exactly one")
        return found[0]

    return lookup


def seconds(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def local_after(run_dir: Path, attempts: list[dict]) -> dict[tuple[str, str], float]:
    """(task_id, attempt recorded_at) -> tool seconds its turn recorded, from the transcripts."""
    brackets: dict[str, list[tuple[float, float, dict[int, float]]]] = {}
    for path in sorted((run_dir / "transcripts").glob("*.jsonl")):
        for trajectory in read_trajectories(path):
            begun = seconds(str(trajectory.start["started_at"]))
            ended = seconds(str(trajectory.end["ended_at"])) if trajectory.end else float("inf")
            per_turn: dict[int, float] = {}
            for event in trajectory.tool_results:
                if event.get("elapsed_s") is not None:
                    turn = int(event["turn"])
                    per_turn[turn] = per_turn.get(turn, 0.0) + float(event["elapsed_s"])
            brackets.setdefault(trajectory.task_id, []).append((begun, ended, per_turn))
    out: dict[tuple[str, str], float] = {}
    for attempt in attempts:
        task, stamp = str(attempt["task_id"]), str(attempt["recorded_at"])
        at = seconds(stamp)
        owners = [b for b in brackets.get(task, []) if b[0] <= at <= b[1]]
        if len(owners) != 1:
            raise SystemExit(f"{task} attempt at {stamp} sits in {len(owners)} brackets")
        out[(task, stamp)] = owners[0][2].get(int(attempt["turn"]), 0.0)
    return out


def main() -> int:
    config = ClientConfig.load(REPO / "config" / "providers.toml")
    lookup = limits_for(config)
    walls = list(read_rows(REPO / WALLS))
    runs = []
    models: dict[str, Limits] = {}
    for agent, run_id, has_transcripts in RUNS:
        run_dir = REPO / "runs" / run_id
        rows = list(read_rows(run_dir / "ledger.jsonl"))
        attempts = [r for r in rows if r.get("kind") == "attempt"]
        local = local_after(run_dir, attempts) if has_transcripts else {}
        sessions = sessions_from_ledger(rows, walls, limits_for=lookup, local_after=local)
        (model,) = {str(a["model"]) for a in attempts}
        (provider,) = {str(a["provider"]) for a in attempts}
        models[model] = lookup(model)
        meta = {
            "agent": agent,
            "run_id": run_id,
            "ledger": f"runs/{run_id}/ledger.jsonl",
            "provider": provider,
            "model": model,
            "date": sessions[0].started_at[:10],
            "transcripts": f"runs/{run_id}/transcripts" if has_transcripts else None,
        }
        runs.append((meta, sessions))
    measurement = {
        "item": "6.1",
        "reading": "docs/PERFORMANCE.md, fixed in d027fc7 before any ratio but A0's was seen",
        "headline": HEADLINE,
        "computed_on": date.today().isoformat(),
        "quota_walls": WALLS,
        "limits_from": "config/providers.toml",
        "tolerance_s": TOLERANCE_S,
        "sensitivity_s": list(SENSITIVITY_S),
        "note": (
            "A measurement of the scheduler, not of the providers' tiers. Every figure is read "
            "from ledgers already written; no request was spent."
        ),
    }
    written = document(measurement, models, runs)
    OUT.write_text(dumps(written) + "\n")
    for run in written["runs"]:
        summary = run["summary"]
        print(
            f"{run['agent']:9} {run['run_id']}  running {summary['running_s']} s  ceiling "
            f"{summary['ceiling_s']} s  ratio {summary['ratio']}  "
            f"({summary['achieved_tasks_per_minute']} vs {summary['ceiling_tasks_per_minute']} "
            "tasks/min)"
        )
    print(f"wrote {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
