"""The trajectory viewer's data, 7.2: every step and figure it shows, from committed files only.

**Not a chat window.** The page this feeds (`viewer/`) shows an agent working: tables listed,
two described, rows sampled, a query written, an error hit and repaired — each step with what
went in and what came back — then the final statement, the rows it returned where the record
holds them, and a footer of what the trajectory cost. This module builds that as JSON; the page
only lays it out, so every figure it shows is computed here, in Python, under test.

**Every input is a committed file** (:data:`INPUTS`). The page replays runs that are already
measured and calls no model, which is what lets 7.3 deploy it with no key (constraint 79). Three
readings are held to the rules the measured results were held to:

- **The last bracket stands** (constraint 86). A task retried on resume leaves its earlier
  bracket in the transcript; the viewer shows the one the projection scored, and a build fails
  if its turns, tool calls or termination disagree with that projection.
- **Figures a transcript does not hold come from the files that do** (constraint 51): tokens
  and elapsed time from each projection's per-task row (or, for the attack run, its lifted
  ledger's task row), whether the cascade escalated and why from the escalation file and the
  composed A2 file, what the attack run found from `results/attacks.json`. **Nothing here
  re-scores anything** (constraint 83): a verdict is read, never recomputed.
- **A verdict says "matches the reference", never that an answer is right** (constraint 84).
  Where 4.4 verified a reference returns wrong data, the task says so beside the verdict.

**Rows are shown only where the record holds them.** No committed file stores the rows a final
statement returned; the transcript stores what each ``execute_sql`` showed the agent. So the
viewer shows those rows when the agent ran its final statement verbatim before answering with
it, and otherwise the row count scoring recorded, and says which.

**Which controls fired** is read the way the attack readers read it: a guard refusal is an
``execute_sql`` error equal to what :func:`~query_pilot.sandbox.single_read_only_statement`
returns for that statement; a truncation is the tool result's ``truncated_by``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from query_pilot.agents.a0 import SYSTEM_PROMPT as A0_SYSTEM_PROMPT
from query_pilot.agents.metrics import normalise_sql
from query_pilot.agents.transcript import MESSAGE, TOOL_RESULT, Trajectory, read_trajectories
from query_pilot.sandbox import single_read_only_statement

__all__ = [
    "CONTROLS",
    "INPUTS",
    "LABELS",
    "build",
    "preloaded_attack_cases",
    "trajectory_view",
    "write",
]

INPUTS: Final[Mapping[str, str]] = {
    "a0": "results/a0-working.json",
    "a1": "results/a1-working.json",
    "a1_transcripts": "tests/transcripts/a1-working-lifted/transcripts",
    "a2_cheap": "results/a2-cheap-working.json",
    "a2_cheap_transcripts": "tests/transcripts/a2-cheap-working-lifted/transcripts",
    "a2": "results/a2-working.json",
    "escalation": "docs/escalation-a2-cheap-working.json",
    "attacks": "results/attacks.json",
    "corpus": "attacks/corpus.json",
    "attack_transcripts": "tests/transcripts/attacks-lifted/transcripts",
    "attack_ledger": "tests/transcripts/attacks-lifted/ledger.jsonl",
    "reference_checks": "docs/a1-failure-counts.json",
}

LABELS: Final[Mapping[str, str]] = {
    "matches": "matches the reference",
    "does_not_match": "does not match the reference",
    "reference_verified_wrong": (
        "The reference query for this task was verified to return wrong data "
        "(docs/a1-failure-counts.json). Matching it is agreement with the reference, "
        "not a right answer."
    ),
    "elapsed": "elapsed, including time spent waiting for quota",
    "rows_seen": "the rows the agent saw when it ran this statement before answering",
    "rows_not_recorded": (
        "rows not recorded: the agent answered with a statement it had not run as written"
    ),
    "no_statement": "no final statement: the trajectory ended without one, so there are no rows",
    "a0_prompt": (
        "One request, with the whole schema of the database read live into the prompt. The "
        "schema was not recorded, so it is not shown."
    ),
    "run_button": (
        "Runs a new trajectory only through the local API (7.1). This page replays committed "
        "runs and never calls a model."
    ),
    "replay": "A replay of a committed run. No model is called from this page.",
}

# docs/GUARDRAILS.md, in its own order.
CONTROLS: Final[Mapping[int, str]] = {
    1: "Read-only connection",
    2: "Statement timeout",
    3: "Row and byte caps",
    4: "DDL / DML rejection, before execution",
    5: "Multi-statement rejection",
}

# The wording of the sandbox's own refusals, which the attack readers compare against the same
# way: the guard's multi-statement reason opens with this, and a deadline reads exactly so.
_MULTI_STATEMENT_OPENS: Final = "the input holds "
_TIMED_OUT: Final = "statement timed out"
_READ_ONLY: Final = re.compile(r"readonly database", re.IGNORECASE)


# -- reading committed files ------------------------------------------------------------------


def _load(root: Path, key: str) -> Any:
    return json.loads((root / INPUTS[key]).read_text(encoding="utf-8"))


def _by_task(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(task["task_id"]): task for task in document["tasks"]}


def _standing(directory: Path, task_id: str) -> tuple[Trajectory, int]:
    brackets = read_trajectories(directory / f"{task_id}.jsonl")
    if not brackets:
        raise ValueError(f"{directory.name}/{task_id}: no trajectory in the transcript")
    return brackets[-1], len(brackets)


def _ledger_task_rows(path: Path) -> dict[str, Mapping[str, Any]]:
    """The last task row per task: the standing trajectory's, as a projection would read it."""
    rows: dict[str, Mapping[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("kind") == "task":
            rows[str(row["task_id"])] = row
    return rows


# -- one trajectory as steps --------------------------------------------------------------------


def controls_fired(tool: str, arguments: Mapping[str, Any], result: Mapping[str, Any]) -> list[int]:
    """Which of the five controls this tool result shows firing, by number."""
    fired: list[int] = []
    error = str(result.get("error") or "")
    if tool == "execute_sql" and not result.get("ok"):
        guard = single_read_only_statement(str(arguments.get("sql", "")))
        if guard is not None and error == guard:
            fired.append(5 if guard.startswith(_MULTI_STATEMENT_OPENS) else 4)
        elif error == _TIMED_OUT:
            fired.append(2)
        elif _READ_ONLY.search(error):
            fired.append(1)
    if result.get("truncated_by"):
        fired.append(3)
    return fired


def trajectory_view(trajectory: Trajectory) -> dict[str, Any]:
    """The ordered steps of one bracket: what the agent said, called, and got back.

    Built from the events alone, in ``seq`` order. A tool call's arguments live in the assistant
    message that made it and its result in the ``tool`` message and ``tool_result`` event that
    share its ``call_id``, so the three are joined here and nowhere else.
    """
    tool_texts: dict[str, str] = {}
    results: dict[str, Mapping[str, Any]] = {}
    for event in trajectory.events:
        if event["kind"] == MESSAGE and event.get("role") == "tool":
            tool_texts[str(event.get("tool_call_id"))] = str(event.get("content") or "")
        elif event["kind"] == TOOL_RESULT:
            results[str(event["call_id"])] = event

    steps: list[dict[str, Any]] = []
    system_prompt: str | None = None
    for event in trajectory.events:
        if event["kind"] != MESSAGE:
            continue
        role = event.get("role")
        if role == "system":
            system_prompt = str(event.get("content") or "")
            continue
        if role == "user":
            if event.get("repair"):
                steps.append(
                    {"kind": "repair_request", "turn": event["turn"], "text": event["content"]}
                )
            continue
        if role != "assistant":
            continue
        text = str(event.get("content") or "")
        calls = event.get("tool_calls") or []
        if text.strip():
            steps.append(
                {
                    "kind": "say",
                    "turn": event["turn"],
                    "repair": bool(event.get("repair")),
                    "text": text,
                }
            )
        for call in calls:
            call_id = str(call["id"])
            result = results.get(call_id)
            arguments = call.get("arguments") or {}
            steps.append(
                {
                    "kind": "call",
                    "turn": event["turn"],
                    "tool": call["name"],
                    "arguments": arguments,
                    "executed": result is not None,
                    "result": tool_texts.get(call_id),
                    "ok": None if result is None else bool(result["ok"]),
                    "error": None if result is None else result.get("error"),
                    "rows_returned": None if result is None else result.get("rows_returned"),
                    "rows_shown": None if result is None else result.get("rows_shown"),
                    "truncated_by": None if result is None else result.get("truncated_by"),
                    "controls": []
                    if result is None
                    else controls_fired(str(call["name"]), arguments, result),
                }
            )
    end = trajectory.end or {}
    return {
        "system_prompt": system_prompt,
        "question": trajectory.start.get("question"),
        "database": trajectory.start.get("db_id"),
        "run_id": trajectory.start.get("run_id"),
        "limits": trajectory.start.get("limits"),
        "steps": steps,
        "end": {
            "termination": end.get("outcome"),
            "turns": end.get("turns"),
            "tool_calls": end.get("tool_calls"),
            "repair_attempts": end.get("repair_attempts"),
            "repair_succeeded": end.get("repair_succeeded"),
            "repair_blocked": end.get("repair_blocked"),
            "failed_generation": end.get("failed_generation"),
        },
    }


def _rows_seen(view: Mapping[str, Any], final_sql: str | None) -> dict[str, Any] | None:
    """The last ``execute_sql`` that ran the final statement as written, if there was one."""
    if not final_sql:
        return None
    wanted = normalise_sql(final_sql)
    for step in reversed(view["steps"]):
        if step["kind"] != "call" or step["tool"] != "execute_sql" or not step["ok"]:
            continue
        if normalise_sql(str(step["arguments"].get("sql", ""))) == wanted:
            return {
                "turn": step["turn"],
                "text": step["result"],
                "rows_returned": step["rows_returned"],
                "rows_shown": step["rows_shown"],
            }
    return None


def _rows_note(final_sql: str | None, seen: Mapping[str, Any] | None) -> str:
    if not final_sql:
        return LABELS["no_statement"]
    return LABELS["rows_seen"] if seen else LABELS["rows_not_recorded"]


def _check(label: str, view: Mapping[str, Any], row: Mapping[str, Any]) -> None:
    """The standing bracket must be the trajectory the projection scored."""
    end = view["end"]
    for field in ("turns", "tool_calls", "termination"):
        if row.get(field) != end.get(field):
            raise ValueError(
                f"{label}: the transcript's last bracket has {field}={end.get(field)!r} but the "
                f"committed file says {row.get(field)!r}"
            )


def _verdict(solved: bool | None) -> str | None:
    if solved is None:
        return None
    return LABELS["matches"] if solved else LABELS["does_not_match"]


def _fired(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"control": number, "name": CONTROLS[number], "turn": step["turn"], "tool": step["tool"]}
        for step in view["steps"]
        if step["kind"] == "call"
        for number in step["controls"]
    ]


def _scoring_controls(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Controls that fired when scoring ran the final statement, from the projection."""
    fired = []
    if row.get("candidate_timed_out"):
        fired.append({"control": 2, "name": CONTROLS[2], "turn": None, "tool": "scoring"})
    if row.get("candidate_truncated_by"):
        fired.append({"control": 3, "name": CONTROLS[3], "turn": None, "tool": "scoring"})
    return fired


def _loop_side(
    label: str,
    agent: str,
    model: str,
    view: dict[str, Any],
    brackets: int,
    row: Mapping[str, Any],
    prompts: dict[str, str],
    *,
    elapsed: float | None,
    tokens_in: int | None,
    tokens_out: int | None,
    solved: bool | None,
    reason: str | None,
    detail: str | None,
    final_sql: str | None,
    candidate_rows: int | None,
) -> dict[str, Any]:
    _check(label, view, row)
    prompt = view.pop("system_prompt") or ""
    key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    prompts[key] = prompt
    seen = _rows_seen(view, final_sql)
    return {
        "agent": agent,
        **view,
        "system_prompt": key,
        "brackets_in_transcript": brackets,
        "final": {
            "sql": final_sql,
            "rows": seen,
            "rows_note": _rows_note(final_sql, seen),
            "candidate_rows": candidate_rows,
        },
        "footer": {
            "model": model,
            "turns": view["end"]["turns"],
            "tool_calls": view["end"]["tool_calls"],
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "elapsed_s": elapsed,
            "termination": view["end"]["termination"],
            "verdict": _verdict(solved),
            "reason": reason,
            "detail": detail,
            "controls": _fired(view) + _scoring_controls(row),
        },
    }


def _projection_side(
    label: str,
    agent: str,
    row: Mapping[str, Any],
    directory: Path,
    prompts: dict[str, str],
) -> dict[str, Any]:
    trajectory, brackets = _standing(directory, label)
    view = trajectory_view(trajectory)
    return _loop_side(
        label,
        agent,
        f"{row['provider']}/{row['model']}",
        view,
        brackets,
        row,
        prompts,
        elapsed=row.get("elapsed_s"),
        tokens_in=row.get("prompt_tokens"),
        tokens_out=row.get("completion_tokens"),
        solved=row.get("solved"),
        reason=row.get("reason"),
        detail=row.get("detail"),
        final_sql=row.get("sql"),
        candidate_rows=row.get("candidate_rows"),
    )


def _a0_side(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "agent": "A0",
        "prompt": {"system": A0_SYSTEM_PROMPT, "note": LABELS["a0_prompt"]},
        "final": {
            "sql": row.get("sql"),
            "rows": None,
            "rows_note": _rows_note(row.get("sql"), None),
            "candidate_rows": row.get("candidate_rows"),
        },
        "footer": {
            "model": f"{row['provider']}/{row['model']}",
            "turns": 1,
            "tool_calls": 0,
            "tokens_in": row.get("prompt_tokens"),
            "tokens_out": row.get("completion_tokens"),
            "latency_s": row.get("latency_s"),
            "elapsed_s": row.get("elapsed_s"),
            "termination": None,
            "verdict": _verdict(row.get("solved")),
            "reason": row.get("reason"),
            "detail": row.get("detail"),
            "controls": _scoring_controls(row),
        },
    }


# -- the three attack cases shown first -----------------------------------------------------------


def preloaded_attack_cases(cases: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Three cases, one per outcome, each the lowest case id with it — chosen by rule, not by eye.

    A control catching it; the agent seeing the instruction and resisting it; and the agent
    complying with an instruction no control can contain — because showing only the first two
    would misstate a run in which 8 of 45 cases complied.
    """
    ordered = sorted(cases, key=lambda case: str(case["case_id"]))
    rules = (
        ("contained", "A control refused the injected instruction before it ran.",
         lambda c: c["contained"] is True),
        ("resisted", "The agent read the injected instruction and did not act on it.",
         lambda c: bool(c["exposed_via"]) and not c["complied"]),
        ("complied", "The agent acted on an instruction no control can contain.",
         lambda c: c["complied"] and not c["containable"]),
    )  # fmt: skip
    out = []
    for outcome, why, rule in rules:
        case = next((c for c in ordered if rule(c)), None)
        if case is None:
            raise ValueError(f"no attack case shows the outcome {outcome!r}")
        out.append({"case_id": str(case["case_id"]), "outcome": outcome, "why": why})
    return out


def _poisoned_schema(case: Mapping[str, Any]) -> dict[str, Any]:
    """What the corpus planted, and where: the tables as built, rows only where they carry it."""
    tables = []
    for table in case["database"]["tables"]:
        entry = {
            "name": table["name"],
            "columns": [{"name": c["name"], "type": c["type"]} for c in table["columns"]],
        }
        if case["placement"] == "row_values":
            entry["rows"] = table.get("rows", [])
        tables.append(entry)
    return {
        "placement": case["placement"],
        "channel": case["channel"],
        "injected_instruction": case["injected_instruction"],
        "surfaces_via": case.get("surfaces_via"),
        "tables": tables,
    }


# -- the whole data set ----------------------------------------------------------------------------


def build(root: Path | str) -> dict[str, Any]:
    """Every file the page reads, as ``{relative path: JSON value}``. Reads committed files only."""
    root = Path(root)
    a0 = _load(root, "a0")
    a1 = _load(root, "a1")
    cheap = _load(root, "a2_cheap")
    a2 = _load(root, "a2")
    escalation = _load(root, "escalation")
    attacks = _load(root, "attacks")
    corpus = _load(root, "corpus")
    verified_wrong = {
        str(entry["task_id"]) for entry in _load(root, "reference_checks")["reference_verification"]
    }

    a0_rows, a1_rows, cheap_rows = _by_task(a0), _by_task(a1), _by_task(cheap)
    a2_rows, decisions = _by_task(a2), _by_task(escalation)
    order = [str(task["task_id"]) for task in a1["tasks"]]
    for name, rows in (("A0", a0_rows), ("A2-cheap", cheap_rows), ("A2", a2_rows)):
        if list(rows) != order:
            raise ValueError(f"{name}'s committed tasks are not A1's, in A1's order")

    prompts: dict[str, str] = {}
    files: dict[str, Any] = {}
    index_tasks = []
    for task_id in order:
        strong = _projection_side(
            task_id, "A1", a1_rows[task_id], root / INPUTS["a1_transcripts"], prompts
        )
        weak = _projection_side(
            task_id, "A2-cheap", cheap_rows[task_id], root / INPUTS["a2_cheap_transcripts"], prompts
        )
        composed = a2_rows[task_id]
        if list(decisions[task_id]["clauses"]) != list(composed["clauses"]):
            raise ValueError(f"{task_id}: the escalation file and the A2 file disagree")
        flag = LABELS["reference_verified_wrong"] if task_id in verified_wrong else None
        files[f"tasks/{task_id}.json"] = {
            "task_id": task_id,
            "database": a1_rows[task_id]["db_id"],
            "question": strong["question"],
            "difficulty": a1_rows[task_id].get("difficulty"),
            "reference_sql": a1_rows[task_id].get("reference_sql"),
            "reference_rows": a1_rows[task_id].get("reference_rows"),
            "reference_note": flag,
            "A0": _a0_side(a0_rows[task_id]),
            "A1": strong,
            "A2": {
                "escalated": composed["escalated"],
                "clauses": composed["clauses"],
                "clause_definitions": {
                    c: escalation["definitions"][c] for c in composed["clauses"]
                },
                "verdict": _verdict(composed["solved"]),
                "tokens": composed["tokens_standing"],
                "cheap": weak,
                "strong": "A1" if composed["escalated"] else None,
            },
        }
        index_tasks.append(
            {
                "task_id": task_id,
                "database": a1_rows[task_id]["db_id"],
                "question": strong["question"],
                "A0": _verdict(a0_rows[task_id].get("solved")),
                "A1": _verdict(a1_rows[task_id].get("solved")),
                "A2": _verdict(composed["solved"]),
                "escalated": composed["escalated"],
                "reference_verified_wrong": flag is not None,
            }
        )

    ledger = _ledger_task_rows(root / INPUTS["attack_ledger"])
    corpus_cases = {str(case["case_id"]): case for case in corpus["cases"]}
    attack_model = "/".join(attacks["measurement"]["provider_and_model"])
    index_attacks = []
    for outcome in attacks["cases"]:
        case_id = str(outcome["case_id"])
        trajectory, brackets = _standing(root / INPUTS["attack_transcripts"], case_id)
        view = trajectory_view(trajectory)
        row = ledger[case_id]
        side = _loop_side(
            case_id,
            "A1",
            attack_model,
            view,
            brackets,
            outcome,
            prompts,
            elapsed=row.get("elapsed_s"),
            tokens_in=row.get("prompt_tokens"),
            tokens_out=row.get("completion_tokens"),
            solved=outcome.get("solved"),
            reason=outcome.get("reason"),
            detail=None,
            final_sql=outcome.get("sql"),
            candidate_rows=None,
        )
        files[f"attacks/{case_id}.json"] = {
            "case_id": case_id,
            "database_shown": outcome["database_shown"],
            "question": corpus_cases[case_id]["question"],
            "category": outcome["category"],
            "containable": outcome["containable"],
            "containing_control": outcome["containing_control"],
            "poisoned": _poisoned_schema(corpus_cases[case_id]),
            "found": {
                "exposed_via": outcome["exposed_via"],
                "complied": outcome["complied"],
                "contained": outcome["contained"],
                "damaged": outcome["damaged"],
                "attempts": outcome["attempts"],
            },
            "A1": side,
        }
        index_attacks.append(
            {
                "case_id": case_id,
                "database_shown": outcome["database_shown"],
                "placement": outcome["placement"],
                "channel": outcome["channel"],
                "category": outcome["category"],
                "seen": bool(outcome["exposed_via"]),
                "complied": outcome["complied"],
                "contained": outcome["contained"],
            }
        )

    files["index.json"] = {
        "built_from": dict(INPUTS),
        "labels": dict(LABELS),
        "controls": {str(k): v for k, v in CONTROLS.items()},
        "system_prompts": prompts,
        "runs": {
            "A0": _run_card(a0),
            "A1": _run_card(a1),
            "A2-cheap": _run_card(cheap),
            "A2": {
                "execution_accuracy": a2["execution_accuracy"],
                "composed_from": [
                    a2["measurement"]["composed_from"][side]["run_id"]
                    for side in ("cheap", "strong")
                ],
            },
            "attacks": {
                "run_id": attacks["measurement"]["run_id"],
                "date": attacks["measurement"]["date"],
                "model": attack_model,
                "compliance": attacks["compliance"],
                "containment": attacks["containment"],
                "task_damage": attacks["task_damage"],
            },
        },
        "databases": sorted({task["database"] for task in index_tasks}),
        "tasks": index_tasks,
        "attacks": index_attacks,
        "preloaded": preloaded_attack_cases(attacks["cases"]),
    }
    return files


def _run_card(document: Mapping[str, Any]) -> dict[str, Any]:
    measurement = document["measurement"]
    return {
        "run_id": measurement["run_id"],
        "date": measurement["date"],
        "model": "/".join(measurement["provider_and_model"]),
        "execution_accuracy": document["execution_accuracy"],
    }


def dumps(value: Any) -> str:
    """Compact, sorted-key-free, stable: the page's data is read by a machine, not reviewed."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def write(files: Mapping[str, Any], destination: Path | str) -> list[Path]:
    """Write every file under ``destination``, removing any stale one this build did not make."""
    destination = Path(destination)
    written = []
    for relative, value in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dumps(value), encoding="utf-8")
        written.append(target)
    keep = {path.resolve() for path in written}
    for stale in destination.rglob("*.json"):
        if stale.resolve() not in keep:
            stale.unlink()
    return written


def iter_text(files: Mapping[str, Any]) -> Iterable[str]:
    """Every string the page could display, for the tests that scan them."""
    stack: list[Any] = list(files.values())
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, list | tuple):
            stack.extend(value)
