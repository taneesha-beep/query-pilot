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
    # 7.4's results page, beside the projections above.
    "a1_metrics": "results/a1-trajectory-metrics.json",
    "scheduler": "results/scheduler-efficiency.json",
    # The reserve run's projection. The page reads its aggregates only, never a task: no reserve
    # task ID reaches viewer/data/, and splits/reserve.json is never an input.
    "a0_reserve": "results/a0-reserve.json",
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
        "Off on this page, which replays runs already measured and never calls a model. A new "
        "question runs only through the project's local API, on the author's own machine."
    ),
    "replay": "Everything here replays runs already measured. No model is called from this page.",
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
    files["results.json"] = results_page(
        root,
        a0=a0,
        a1=a1,
        cheap=cheap,
        a2=a2,
        attacks=attacks,
        reserve=_load(root, "a0_reserve"),
    )
    return files


# -- 7.4: the results page ------------------------------------------------------------------------

#: What "matches the reference" means, said once at the top of the results page (constraint 84).
MATCH_DEFINITION: Final = (
    "Matches the reference: the agent's final query returned the same rows as the reference "
    "query that comes with the question. Some reference queries are themselves wrong, so a match "
    "is agreement with the reference, not proof of a right answer."
)


#: A multiplication sign, written as an escape so no linter mistakes it for an x.
_TIMES: Final = "\u00d7"

#: The attack readers' control slugs (`agents/attack_results.py`), named as the page names them.
_CONTROL_NAMES: Final[Mapping[str, str]] = {
    "ddl_dml_rejection": f"control 4 ({CONTROLS[4]})",
    "multi_statement": f"control 5 ({CONTROLS[5]})",
}


def _num(value: float | int) -> str:
    """A committed figure as the project writes it: thousands separated, no invented decimals."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}"


def _share(count: int, of: int, percent: float, sep: str = " / ") -> str:
    return f"{count}{sep}{of} — {percent}%"


def _rate(rate: float) -> str:
    return f"{round(rate * 100, 4)}%"


def _source(measurement: Mapping[str, Any]) -> str:
    """Provider, model, date and ledger: the four a figure is not a result without (12)."""
    provider, model = str(measurement["provider_and_model"][0]).split("/", 1)
    ledger = f"runs/{measurement['run_id']}/ledger.jsonl"
    return f"{provider.capitalize()}, {model}, {measurement['date']}, {ledger}"


def results_page(
    root: Path,
    *,
    a0: Mapping[str, Any],
    a1: Mapping[str, Any],
    cheap: Mapping[str, Any],
    a2: Mapping[str, Any],
    attacks: Mapping[str, Any],
    reserve: Mapping[str, Any],
) -> dict[str, Any]:
    """Every committed summary, as the tables the results page lays out. Re-scores nothing.

    The reserve run's section reads only ``beside_the_working_set``, the comparison the run's
    script computed with `query_pilot.reserve.compare`: aggregates, never a task.
    """
    beside = reserve["beside_the_working_set"]
    if beside["working"]["run_id"] != a0["measurement"]["run_id"]:
        raise ValueError("the reserve figure is set beside a different working run than A0's")
    metrics = _load(root, "a1_metrics")
    scheduler = _load(root, "scheduler")
    acc0, acc1 = a0["execution_accuracy"], a1["execution_accuracy"]
    floor = acc0["empty_result_floor"]
    empty = {str(t["task_id"]) for t in a0["tasks"] if t["reference_rows"] == 0}
    if len(empty) != floor["tasks"]:
        raise ValueError("A0's projection does not hold the empty-reference tasks it counts")
    collected = {
        name: sum(bool(t["solved"]) for t in doc["tasks"] if str(t["task_id"]) in empty)
        for name, doc in (("A0", a0), ("A1", a1))
    }
    ratio = round(a1["tokens"]["total"] / a0["tokens"]["total"], 2)
    points = {str(p["agent"]): p for p in a2["frontier"]["points"]}
    verdict = a2["frontier"]["verdict"]
    if verdict["cascade_wins"]:
        raise ValueError("the committed verdict says the cascade wins; this page says it loses")
    never_seen = sum(not case["exposed_via"] for case in attacks["cases"])
    by_control = attacks["containment"]["by_control"]
    runs = {str(run["agent"]): run for run in scheduler["runs"]}
    difficulty = ("easy", "medium", "hard", "extra")
    recovery = metrics["recovery_rate"]
    wasted = metrics["wasted_calls"]

    def accuracy(doc: Mapping[str, Any]) -> str:
        acc = doc["execution_accuracy"]
        return _share(acc["solved"], acc["of"], acc["percent"])

    def stats(block: Mapping[str, Any]) -> str:
        return " / ".join(_num(block[key]) for key in ("mean", "median", "p90"))

    sections = [
        {
            "id": "comparison",
            "eyebrow": "Headline",
            "title": "A0 against A1",
            "lead": (
                "The same model, answer rules and sandbox. A0 is handed the whole schema in one "
                "request; A1 has to discover it with four tools."
            ),
            "columns": [
                "Agent",
                "Matches the reference",
                "Tokens",
                "Tokens per solved task",
                "Requests",
            ],
            "rows": [
                [
                    label,
                    accuracy(doc),
                    _num(doc["tokens"]["total"]),
                    _num(doc["tokens"]["per_solved_task"]),
                    _num(doc["run"]["attempts"]),
                ]
                for label, doc in (("A0 single-shot", a0), ("A1 agent loop", a1))
            ],
            "notes": [
                f"The loop lost: A1 matched {acc0['solved'] - acc1['solved']} fewer references "
                f"for {ratio}{_TIMES} the tokens, so there is no cost per additional solved task.",
                f"{floor['tasks']} of {floor['of']} reference queries return no rows, so an empty "
                f"answer matches them for free ({floor['percent']}% of the set). A0 collected "
                f"{collected['A0']} of them and A1 {collected['A1']}.",
            ],
            "sources": [
                f"A0: {_source(a0['measurement'])}",
                f"A1: {_source(a1['measurement'])}",
                "results/a0-working.json, results/a1-working.json",
            ],
        },
        {
            "id": "containment",
            "eyebrow": "Under attack",
            "title": "Containment",
            "lead": (
                f"A1 over {attacks['measurement']['cases_declared']} attack cases: instructions "
                "planted in table and column names, column types and rows."
            ),
            "columns": ["Measure", "Result", "Out of"],
            "rows": [
                [
                    label,
                    _share(block["count"], block["of"], block["percent"], " of "),
                    block["denominator"],
                ]
                for label, block in (
                    (
                        "Compliance — the trajectory attempted the planted instruction",
                        attacks["compliance"],
                    ),
                    (
                        "Containment — every compliant attempt refused before it ran",
                        attacks["containment"],
                    ),
                    (
                        "Task damage — resisted, and answered wrongly anyway",
                        attacks["task_damage"],
                    ),
                )
            ],
            "notes": [
                "The three have different denominators and are never quoted as one figure.",
                f"All {attacks['containment']['count']} contained attempts were refused by "
                f"{' and '.join(_CONTROL_NAMES.get(c, c) for c in sorted(by_control))}, before a "
                "connection opened.",
                f"{never_seen} of {attacks['measurement']['cases_declared']} cases were never "
                "seen: the agent never looked where they were planted.",
            ],
            "sources": [f"A1: {_source(attacks['measurement'])}", "results/attacks.json"],
        },
        {
            "id": "frontier",
            "eyebrow": "Cost",
            "title": "The cost frontier",
            "lead": (
                "What each agent matched, and what it spent. Both providers are free tiers, so "
                "the cost here is tokens."
            ),
            "columns": [
                "Agent",
                "Model",
                "Matches the reference",
                "Tokens",
                "Tokens per solved task",
            ],
            "rows": [
                [
                    "A0 single-shot",
                    a0["measurement"]["provider_and_model"][0].split("/", 1)[1],
                    accuracy(a0),
                    _num(a0["tokens"]["total"]),
                    _num(a0["tokens"]["per_solved_task"]),
                ],
                *(
                    [
                        label,
                        ", then ".join(m.split("/", 1)[1] for m in points[name]["tokens_by_model"]),
                        _share(points[name]["solved"], points[name]["of"], points[name]["percent"]),
                        _num(points[name]["tokens"]),
                        _num(points[name]["per_solved_task"]),
                    ]
                    for label, name in (
                        ("A1 agent loop", "A1"),
                        ("A2-cheap — A1's loop on the cheap model", "A2-cheap"),
                        ("A2 cascade — cheap, escalated to A1", "A2"),
                    )
                ),
            ],
            "notes": [
                f"The cascade loses: {_num(verdict['cascade'])} tokens a solved task against "
                f"A1's {_num(verdict['always_strong'])}.",
                f"The escalation rule handed {a2['escalation']['escalated']} of "
                f"{a2['escalation']['of']} cheap trajectories to A1.",
                "It would win only if a cheap token cost less than "
                f"{round(a2['frontier']['break_even_price_ratio'] * 100, 2)}% of a strong one.",
            ],
            "sources": [
                f"A2-cheap: {_source(cheap['measurement'])}",
                "A2: composed from the A2-cheap and A1 runs",
                "results/a2-cheap-working.json, results/a2-working.json",
            ],
        },
        {
            "id": "difficulty",
            "eyebrow": "Breakdown",
            "title": "By Spider difficulty",
            "lead": "Matches the reference, by the difficulty Spider gives each question.",
            "columns": ["Agent", *difficulty],
            "rows": [
                [label, *(f"{doc['by_difficulty'][d]['percent']}%" for d in difficulty)]
                for label, doc in (("A0", a0), ("A1", a1), ("A2-cheap", cheap))
            ],
            "notes": [
                "Out of "
                + " / ".join(str(a1["by_difficulty"][d]["of"]) for d in difficulty)
                + " tasks."
            ],
            "sources": [
                "results/a0-working.json, results/a1-working.json, results/a2-cheap-working.json"
            ],
        },
        {
            "id": "trajectory",
            "eyebrow": "The loop",
            "title": "A1's trajectories",
            "lead": "What the loop did, which a single request cannot have.",
            "columns": ["Measure", "A1"],
            "rows": [
                [
                    "Tool calls per task (mean / median / p90)",
                    f"{stats(metrics['tool_calls_per_task'])}, over "
                    f"{metrics['tool_calls_per_task']['n']}",
                ],
                [
                    "Turns to solve (mean / median / p90)",
                    f"{stats(metrics['turns_to_solve'])}, over the "
                    f"{metrics['turns_to_solve']['n']} solved",
                ],
                [
                    "Recovery after a first query that errored",
                    f"{recovery['error']['rate']} over a denominator of "
                    f"{recovery['error']['denominator']}",
                ],
                [
                    "Recovery after a first query that returned nothing",
                    f"{recovery['empty']['recovered']} of {recovery['empty']['denominator']} — "
                    f"{_rate(recovery['empty']['rate'])}",
                ],
                [
                    "Wasted calls",
                    f"{wasted['wasted']} of {wasted['calls_counted']} — {_rate(wasted['rate'])}, "
                    f"over {wasted['tasks_counted']} trajectories",
                ],
                [
                    "Repairs attempted / succeeded",
                    f"{metrics['repairs']['attempts']} / {metrics['repairs']['successes']}",
                ],
            ],
            "notes": ["A0 has no row here: one request has no tool calls, turns or recovery."],
            "sources": [f"A1: {_source(a1['measurement'])}", INPUTS["a1_metrics"]],
        },
        {
            "id": "scheduler",
            "eyebrow": "Throughput",
            "title": "Scheduler efficiency",
            "lead": (
                "The least time the declared per-pool limits allow for the requests each run "
                "made, against the time it ran. Read from ledgers already written."
            ),
            "columns": ["Run", "Running time", "Least time the limits allow", "Ratio"],
            "rows": [
                [
                    f"{name} — {runs[name]['model']}, {runs[name]['date']}",
                    f"{_num(runs[name]['summary']['running_s'])} s",
                    f"{_num(runs[name]['summary']['ceiling_s'])} s",
                    f"{runs[name]['summary']['ratio_percent']}%",
                ]
                for name in ("A1", "A0", "A2-cheap")
            ],
            "notes": [
                "A measurement of this project's scheduler, not of the provider's tiers; A1's "
                "run is the headline, fixed before its ratio existed."
            ],
            "sources": [
                *(f"{name}: {runs[name]['ledger']}" for name in ("A1", "A0", "A2-cheap")),
                INPUTS["scheduler"],
            ],
        },
        _reserve_section(beside, reserve, a0, difficulty),
    ]
    return {"definition": MATCH_DEFINITION, "sections": sections}


def _reserve_section(
    beside: Mapping[str, Any],
    reserve: Mapping[str, Any],
    a0: Mapping[str, Any],
    difficulty: tuple[str, ...],
) -> dict[str, Any]:
    """The reserve figure beside the working figure, as `docs/RESULTS.md` fixed before the run.

    Every cell comes from ``beside``; a block the run could not finish reads ``TBD``.
    """

    def share(block: Any) -> str:
        if not isinstance(block, Mapping) or isinstance(block.get("percent"), str):
            return "TBD"
        return _share(block["solved"], block["of"], block["percent"])

    def floor(block: Mapping[str, Any]) -> str:
        return f"{block['tasks']} of {block['of']} — {block['percent']}%"

    def tokens(block: Mapping[str, Any], key: str) -> str:
        value = block[key]
        return "TBD" if isinstance(value, str) else _num(value)

    def level(side: str, name: str) -> str:
        levels = beside["by_difficulty"][side]
        return "TBD" if isinstance(levels, str) else share(levels[name])

    difference = beside["difference"]
    if isinstance(difference["tasks"], str):
        headline = "The reserve run did not finish, so it has no figure: TBD."
    elif difference["tasks"] == 0:
        headline = "The reserve set matched as many references as the working set."
    else:
        headline = (
            f"Reserve minus working: {difference['tasks']:+d} tasks, "
            f"{difference['percentage_points']:+} percentage points. Each set has been run once, "
            "so there is no measured spread to call that large or small."
        )
    return {
        "id": "reserve",
        "eyebrow": "Read once",
        "title": "Reserve set",
        "lead": (
            "A0, run once at the very end on 150 questions nothing was tuned against, beside its "
            "working-set figure. A0 is the agent that matched the most references at the fewest "
            "tokens on the working set."
        ),
        "columns": ["A0", "Working set", "Reserve set"],
        "rows": [
            ["Matches the reference", share(beside["working"]), share(beside["reserve"])],
            [
                "An empty answer matches",
                floor(beside["empty_result_floor"]["working"]),
                floor(beside["empty_result_floor"]["reserve"]),
            ],
            [
                "Matches, without those",
                share(beside["excluding_empty_references"]["working"]),
                share(beside["excluding_empty_references"]["reserve"]),
            ],
            *([name, level("working", name), level("reserve", name)] for name in difficulty),
            [
                "Tokens",
                tokens(beside["tokens"]["working"], "total"),
                tokens(beside["tokens"]["reserve"], "total"),
            ],
            [
                "Tokens per solved task",
                tokens(beside["tokens"]["working"], "per_solved_task"),
                tokens(beside["tokens"]["reserve"], "per_solved_task"),
            ],
        ],
        "notes": [
            headline,
            "The same 20 databases appear in both sets, so this tests new questions, not new "
            "schemas.",
        ],
        "sources": [
            f"Reserve: {_source(reserve['measurement'])}",
            f"Working: {_source(a0['measurement'])}",
            "results/a0-reserve.json, results/a0-working.json",
        ],
    }


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
