"""5.2's cascade, A2: how it is composed, what it costs, and when it wins.

**No test here makes a live API call.** Two small runs are written here as ledgers with the
fields the real writers produce, projected through `agents/results.py` exactly as a committed
result is, and composed. The four tasks are the four cases a cascade can meet:

- ``t1`` — the rule is silent, the cheap model solved it.
- ``t2`` — the rule is silent, the cheap model lost a task the strong one solves. Its cheap
  attempt was retried once, so tokens were spent before the trajectory that stands.
- ``t3`` — the provider refused the cheap model's output (``provider_rejected``); the rule fires
  and the strong model solves it — on a retry, after a refused request of its own.
- ``t4`` — the cheap model's first query returned no rows and it "solved" an empty reference;
  the rule fires anyway and the strong model loses it. The escalation cost a solve.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from query_pilot.agents import project, results_name
from query_pilot.agents.a1 import ANSWER, PROVIDER_REJECTED, TOOL_CALL_LIMIT_REACHED
from query_pilot.agents.cascade import (
    AGENT,
    DEFINITIONS,
    NotComposable,
    Side,
    attempts_by_task,
    compose,
    earlier_attempts,
    refused_requests,
)
from query_pilot.agents.escalation import CLAUSES, decide
from query_pilot.agents.escalation import DEFINITIONS as RULE
from query_pilot.agents.metrics import EMPTY, ROWS
from query_pilot.agents.validate import NO_STATEMENT

CHEAP_MODEL = "openai/gpt-oss-20b"
STRONG_MODEL = "openai/gpt-oss-120b"
STAMP = "2026-09-12T00:00:00+00:00"
SOURCES = {"cheap": "cheap.json", "strong": "strong.json", "escalation": "escalation.json"}


@dataclass(frozen=True)
class Spec:
    """One task of a built run: its standing trajectory, and anything spent before it."""

    task_id: str
    solved: bool
    termination: str = ANSWER
    validation_rule: str | None = None
    first: str | None = ROWS
    attempts: tuple[tuple[int, int], ...] = ()  # (prompt, completion) per standing turn
    earlier: tuple[tuple[int, int], ...] = ()  # a failed bracket's attempts, retried
    earlier_class: str = "unclassified"
    repair_blocked: str | None = None


CHEAP = (
    Spec("t1", True, attempts=((50, 10), (30, 10))),
    Spec("t2", False, attempts=((40, 10),) * 3, earlier=((20, 5), (20, 5))),
    Spec("t3", False, PROVIDER_REJECTED, NO_STATEMENT, first=None, attempts=((70, 10),)),
    Spec("t4", True, first=EMPTY, attempts=((45, 5), (45, 5))),
)
STRONG = (
    Spec("t1", True, attempts=((90, 10),) * 3),
    Spec("t2", True, attempts=((140, 10),) * 2),
    Spec("t3", True, attempts=((90, 10),) * 4, earlier=((25, 5),), earlier_class="bad_request"),
    Spec("t4", False, TOOL_CALL_LIMIT_REACHED, NO_STATEMENT, attempts=((180, 20),) * 5),
)


def _ledger(path: Path, *, run_id: str, agent: str, model: str, specs) -> Path:
    rows: list[dict] = [
        {
            "kind": "run_start",
            "run_id": run_id,
            "agent": agent,
            "started_at": STAMP,
            "declared": {"task_ids": [spec.task_id for spec in specs]},
        }
    ]

    def attempts(spec: Spec, pairs) -> None:
        for turn, (prompt, completion) in enumerate(pairs, start=1):
            rows.append(
                {
                    "kind": "attempt",
                    "run_id": run_id,
                    "task_id": spec.task_id,
                    "turn": turn,
                    "provider": "groq",
                    "model": model,
                    "pool": "groq#1",
                    "outcome": "ok",
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "recorded_at": STAMP,
                }
            )

    for spec in specs:
        if spec.earlier:
            attempts(spec, spec.earlier)
            rows.append(
                {
                    "kind": "task",
                    "run_id": run_id,
                    "task_id": spec.task_id,
                    "status": "failed",
                    "error_class": spec.earlier_class,
                    "detail": {"exception": "X"},
                }
            )
        attempts(spec, spec.attempts)
        rows.append(
            {
                "kind": "task",
                "run_id": run_id,
                "task_id": spec.task_id,
                "status": "complete",
                "detail": {
                    "db_id": "db",
                    "solved": spec.solved,
                    "reason": "solved" if spec.solved else "no_sql",
                    "termination": spec.termination,
                    "validation_rule": spec.validation_rule,
                    "turns": len(spec.attempts),
                    "prompt_tokens": sum(p for p, _ in spec.attempts),
                    "completion_tokens": sum(c for _, c in spec.attempts),
                    "repair_blocked": spec.repair_blocked,
                    **(
                        {"provider_rejection": {"status": 400, "code": "x", "message": "m"}}
                        if spec.termination == PROVIDER_REJECTED
                        else {}
                    ),
                },
            }
        )
    rows.append(
        {
            "kind": "run_end",
            "run_id": run_id,
            "status": "complete",
            "incomplete_reason": None,
            "ended_at": STAMP,
        }
    )
    path.mkdir(parents=True, exist_ok=True)
    ledger = path / "ledger.jsonl"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return ledger


def _side(ledger: Path) -> Side:
    projection = project(ledger, split="working", empty_reference_tasks=1, generated_at=STAMP)
    return Side(
        projection=projection,
        earlier=earlier_attempts(projection, attempts_by_task(ledger)),
        refused=refused_requests(ledger),
    )


def _escalation(specs) -> dict:
    tasks = []
    for spec in specs:
        clauses = decide(
            termination=spec.termination,
            validation_rule=spec.validation_rule,
            first_execute_sql=spec.first,
        ).clauses
        tasks.append(
            {
                "task_id": spec.task_id,
                "termination": spec.termination,
                "validation_rule": spec.validation_rule,
                "first_execute_sql": spec.first,
                "clauses": list(clauses),
                "solved": spec.solved,
            }
        )
    return {"run_id": "cheap-run", "definitions": RULE, "undecided": [], "tasks": tasks}


@pytest.fixture
def sides(tmp_path: Path) -> tuple[Side, Side]:
    cheap = _ledger(
        tmp_path / "c", run_id="cheap-run", agent="A2-cheap", model=CHEAP_MODEL, specs=CHEAP
    )
    strong = _ledger(
        tmp_path / "s", run_id="strong-run", agent="A1", model=STRONG_MODEL, specs=STRONG
    )
    return _side(cheap), _side(strong)


def _compose(cheap: Side, strong: Side, escalation: dict | None = None) -> dict:
    return compose(
        cheap,
        strong,
        escalation if escalation is not None else _escalation(CHEAP),
        sources=SOURCES,
        empty_reference=["t4"],
        wrong_reference=["t2"],
    )


@pytest.fixture
def cascade(sides) -> dict:
    return _compose(*sides)


# --- what the ledgers hold that a projection does not -------------------------------------------


def test_attempts_are_summed_per_task_including_a_retried_bracket(tmp_path) -> None:
    ledger = _ledger(tmp_path, run_id="r", agent="A2-cheap", model=CHEAP_MODEL, specs=CHEAP)
    assert attempts_by_task(ledger) == {
        "t1": {"attempt_rows": 2, "tokens": 100},
        "t2": {"attempt_rows": 5, "tokens": 200},
        "t3": {"attempt_rows": 1, "tokens": 80},
        "t4": {"attempt_rows": 2, "tokens": 100},
    }


def test_earlier_attempts_are_what_the_ledger_holds_beyond_the_standing_trajectory(sides) -> None:
    cheap, strong = sides
    assert cheap.earlier == {"t2": {"attempt_rows": 2, "tokens": 50}}
    assert strong.earlier == {"t3": {"attempt_rows": 1, "tokens": 30}}


def test_a_standing_trajectory_larger_than_the_ledger_is_a_disagreement(sides) -> None:
    cheap, _ = sides
    with pytest.raises(NotComposable, match="more than the ledger holds"):
        earlier_attempts(cheap.projection, {"t1": {"attempt_rows": 1, "tokens": 10}})


def test_an_attempt_row_with_no_task_cannot_be_attributed(tmp_path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"kind": "attempt", "prompt_tokens": 1}) + "\n")
    with pytest.raises(NotComposable, match="names no task"):
        attempts_by_task(ledger)


def test_refused_requests_count_rejections_blocked_repairs_and_bad_request_failures(
    tmp_path,
) -> None:
    specs = (
        Spec("a", False, PROVIDER_REJECTED, NO_STATEMENT, first=None, attempts=((1, 1),)),
        Spec("b", True, attempts=((1, 1),), repair_blocked=PROVIDER_REJECTED),
        Spec("c", True, attempts=((1, 1),), earlier=((1, 1),), earlier_class="bad_request"),
        Spec("d", True, attempts=((1, 1),), earlier=((1, 1),), earlier_class="unclassified"),
        Spec("e", True, attempts=((1, 1),)),
    )
    ledger = _ledger(tmp_path, run_id="r", agent="A2-cheap", model=CHEAP_MODEL, specs=specs)
    # A quota wall or any other failure is not a refused generation; only a 400 is.
    assert refused_requests(ledger) == {"a": 1, "b": 1, "c": 1}


def test_the_projection_counts_provider_rejected_and_keeps_the_providers_prose_out(sides) -> None:
    """How the sixth termination reaches a committed file — with no change to `results.py`."""
    projection = sides[0].projection
    assert projection["terminations"] == {ANSWER: 3, PROVIDER_REJECTED: 1}
    assert projection["validation_rules"] == {NO_STATEMENT: 1}
    rejected = next(task for task in projection["tasks"] if task["task_id"] == "t3")
    assert rejected["termination"] == PROVIDER_REJECTED and rejected["solved"] is False
    assert "provider_rejection" not in rejected
    assert "repair_blocked" in rejected


# --- composing ----------------------------------------------------------------------------------


def test_each_task_takes_the_cheap_outcome_unless_the_rule_fires(cascade) -> None:
    outcome = {t["task_id"]: (t["escalated"], t["solved"]) for t in cascade["tasks"]}
    assert outcome == {
        "t1": (False, True),
        "t2": (False, False),
        "t3": (True, True),
        "t4": (True, False),
    }
    assert cascade["execution_accuracy"]["solved"] == 2
    assert cascade["escalation"]["task_ids"] == ["t3", "t4"]
    assert cascade["escalation"]["clauses"] == {
        CLAUSES[0]: 1,
        CLAUSES[1]: 1,
        CLAUSES[2]: 0,
        CLAUSES[3]: 1,
    }


def test_cost_counts_every_cheap_attempt_and_the_escalated_tasks_strong_ones(cascade) -> None:
    """Cheap: 480 over all four tasks, t2's retried attempts included. Strong: t3's 30 before
    its retry plus 400, and t4's 1,000 — never t1's or t2's, which were not escalated."""
    tokens = cascade["tokens"]
    assert tokens["by_model"] == {f"groq/{CHEAP_MODEL}": 480, f"groq/{STRONG_MODEL}": 1430}
    assert tokens["total"] == 1910
    assert tokens["per_solved_task"] == 955.0
    assert tokens["standing_trajectories_only"] == {
        "by_model": {f"groq/{CHEAP_MODEL}": 430, f"groq/{STRONG_MODEL}": 1400},
        "total": 1830,
        "per_solved_task": 915.0,
    }
    assert tokens["refused_requests"] == {"cheap": 1, "strong": 1}
    assert tokens["money"] == 0.0


def test_the_frontier_holds_all_three_on_the_same_basis(cascade) -> None:
    points = {p["agent"]: p for p in cascade["frontier"]["points"]}
    assert {
        a: (p["solved"], p["tokens"], p["per_solved_task"], p["attempt_rows"])
        for a, p in points.items()
    } == {
        "A2-cheap": (2, 480, 240.0, 10),
        "A1": (3, 2030, 676.7, 15),
        "A2": (2, 1910, 955.0, 20),
    }
    assert points["A2"]["dominated_by"] == ["A2-cheap"]
    assert points["A2-cheap"]["dominated_by"] == points["A1"]["dominated_by"] == []


def test_the_cascade_loses_when_its_cost_per_solved_task_is_not_below_always_strongs(
    cascade,
) -> None:
    verdict = cascade["frontier"]["verdict"]
    assert verdict["cascade_wins"] is False
    assert (verdict["cascade"], verdict["always_strong"]) == (955.0, 676.7)
    assert verdict["tasks_against_always_strong"] == -1
    assert verdict["token_ratio_against_always_strong"] == round(1910 / 2030, 4)
    # (2030 * 2/3 - 1430) / 480: below zero, so no price of a cheap token makes it win.
    assert cascade["frontier"]["break_even_price_ratio"] == round((2030 * 2 / 3 - 1430) / 480, 4)
    assert cascade["frontier"]["break_even_price_ratio"] < 0


def test_the_cascade_wins_when_the_strong_model_spends_most_where_the_rule_is_silent(
    tmp_path,
) -> None:
    strong = list(STRONG)
    strong[0] = Spec("t1", True, attempts=((4000, 1000),) * 3)  # 15,000 the cascade never pays
    s = _ledger(tmp_path / "s", run_id="s", agent="A1", model=STRONG_MODEL, specs=strong)
    c = _ledger(tmp_path / "c", run_id="c", agent="A2-cheap", model=CHEAP_MODEL, specs=CHEAP)
    document = _compose(_side(c), _side(s))
    verdict = document["frontier"]["verdict"]
    # Cascade 1,910 over 2 against always-strong 16,730 over 3: 955.0 against 5,576.7.
    assert verdict["cascade_wins"] is True
    assert document["frontier"]["break_even_price_ratio"] > 1


def test_a_tie_on_cost_per_solved_task_is_not_a_win(tmp_path) -> None:
    """Strictly below, compared as integers: 1,910 x 3 == 2,865 x 2."""
    strong = list(STRONG)
    strong[1] = Spec("t2", True, attempts=((1000, 135),))  # always-strong 2,865 over 3
    s = _ledger(tmp_path / "s", run_id="s", agent="A1", model=STRONG_MODEL, specs=strong)
    c = _ledger(tmp_path / "c", run_id="c", agent="A2-cheap", model=CHEAP_MODEL, specs=CHEAP)
    verdict = _compose(_side(c), _side(s))["frontier"]["verdict"]
    assert verdict["cascade"] == verdict["always_strong"] == 955.0
    assert verdict["cascade_wins"] is False


def test_the_routes_and_the_pairs_say_where_the_difference_came_from(cascade) -> None:
    assert cascade["routes"] == {
        "rule_silent": {"tasks": 2, "cheap_solved": 1, "strong_solved": 2},
        "rule_fired": {"tasks": 2, "cheap_solved": 1, "strong_solved": 1},
    }
    # Paired: on the escalated tasks the cascade IS always-strong, so its one loss against
    # always-strong is t2 — a task the rule left with the cheap model.
    assert cascade["paired"]["against_always_strong"] == {
        "both": 2,
        "A2_only": 0,
        "A1_only": 1,
        "neither": 1,
    }
    assert cascade["paired"]["against_always_cheap"] == {
        "both": 1,
        "A2_only": 1,
        "A2_cheap_only": 1,
        "neither": 1,
    }


def test_the_rows_that_could_move_are_listed_with_all_three_outcomes(cascade) -> None:
    rows = cascade["rows_that_could_move"]
    assert rows["empty_reference"] == [
        {
            "task_id": "t4",
            "db_id": "db",
            "clauses": [CLAUSES[3]],
            "cheap_solved": True,
            "strong_solved": False,
            "solved": False,
        }
    ]
    assert [r["task_id"] for r in rows["verified_wrong_reference"]] == ["t2"]
    assert cascade["execution_accuracy"]["empty_result_floor"] == {
        "tasks": 1,
        "of": 4,
        "percent": 25.0,
    }


def test_the_document_is_named_for_a2_and_reproduces_byte_for_byte(sides, cascade) -> None:
    assert cascade["measurement"]["agent"] == AGENT
    assert results_name(cascade) == "a2-working.json"
    assert json.dumps(_compose(*sides)) == json.dumps(cascade)
    assert cascade["definitions"] == DEFINITIONS
    carried = cascade["measurement"]["composed_from"]
    assert carried["cheap"]["earlier_attempts"] == {"t2": {"attempt_rows": 2, "tokens": 50}}
    assert carried["strong"]["refused_requests"] == {"t3": 1}


# --- what compose refuses -----------------------------------------------------------------------


def test_an_incomplete_run_is_refused(tmp_path, sides) -> None:
    ledger = _ledger(tmp_path / "x", run_id="x", agent="A2-cheap", model=CHEAP_MODEL, specs=CHEAP)
    lines = ledger.read_text().splitlines()
    ledger.write_text("\n".join(lines[:-2]) + "\n")  # t4 never completed, no run_end
    partial = project(ledger, split="working", empty_reference_tasks=1, generated_at=STAMP)
    assert partial["run"]["tasks_complete"] == 3
    with pytest.raises(NotComposable, match="not a complete run"):
        _compose(Side(partial), sides[1])
    # And the attempts t4 made cannot be attributed to a task the projection does not hold.
    with pytest.raises(NotComposable, match="does not hold"):
        earlier_attempts(partial, attempts_by_task(ledger))


def test_a_carried_map_that_does_not_reconcile_with_the_projection_is_refused(sides) -> None:
    cheap, strong = sides
    short = Side(cheap.projection, {"t2": {"attempt_rows": 2, "tokens": 49}}, cheap.refused)
    with pytest.raises(NotComposable, match="the run recorded"):
        _compose(short, strong)
    unrefused = Side(cheap.projection, cheap.earlier, {})
    with pytest.raises(NotComposable, match="no refusal is carried"):
        _compose(unrefused, strong)


def test_a_decision_that_is_not_the_frozen_rules_is_refused(sides) -> None:
    tampered = _escalation(CHEAP)
    tampered["tasks"][3]["clauses"] = []  # t4's empty first query, waved through
    with pytest.raises(NotComposable, match="the rule"):
        _compose(*sides, escalation=tampered)
    retuned = _escalation(CHEAP)
    retuned["definitions"] = {**RULE, "rule": "something else"}
    with pytest.raises(NotComposable, match="not the frozen rule"):
        _compose(*sides, escalation=retuned)


def test_decisions_whose_inputs_disagree_with_the_cheap_run_are_refused(sides) -> None:
    wrong = _escalation(CHEAP)
    wrong["tasks"][0]["solved"] = False
    with pytest.raises(NotComposable, match="disagree"):
        _compose(*sides, escalation=wrong)
    missing = _escalation(CHEAP)
    missing["tasks"] = missing["tasks"][:3]
    with pytest.raises(NotComposable, match="every task"):
        _compose(*sides, escalation=missing)


def test_the_sides_must_be_the_cheap_and_strong_agents_over_the_same_tasks(tmp_path, sides) -> None:
    cheap, strong = sides
    with pytest.raises(NotComposable, match="cheap side"):
        _compose(strong, strong)
    other = _ledger(tmp_path / "o", run_id="o", agent="A1", model=STRONG_MODEL, specs=STRONG[:3])
    with pytest.raises(NotComposable, match="same tasks"):
        _compose(cheap, _side(other))


# --- the committed cascade, regenerated from committed inputs alone -----------------------------

REPO = Path(__file__).resolve().parents[1]
COMMITTED = REPO / "results" / "a2-working.json"
FAILURE_COUNTS = REPO / "docs" / "a1-failure-counts.json"


@pytest.fixture(scope="module")
def committed() -> dict:
    return json.loads(COMMITTED.read_text())


def _committed_side(document: dict, name: str) -> Side:
    """A side rebuilt from its committed projection and the two maps the cascade carries."""
    source = document["measurement"]["composed_from"][name]
    return Side(
        projection=json.loads((REPO / source["results"]).read_text()),
        earlier=source["earlier_attempts"],
        refused=source["refused_requests"],
    )


def test_the_committed_cascade_is_regenerated_whole_from_committed_inputs(committed) -> None:
    """The ledgers are gitignored; what they alone knew is carried, and `compose` refuses a
    carried map that does not reconcile exactly with each committed projection's totals."""
    sources = committed["measurement"]["composed_from"]
    references = json.loads(FAILURE_COUNTS.read_text())["reference_verification"]
    regenerated = compose(
        _committed_side(committed, "cheap"),
        _committed_side(committed, "strong"),
        json.loads((REPO / sources["escalation"]["file"]).read_text()),
        sources={
            "cheap": sources["cheap"]["results"],
            "strong": sources["strong"]["results"],
            "escalation": sources["escalation"]["file"],
        },
        empty_reference=[
            r["task_id"] for r in committed["rows_that_could_move"]["empty_reference"]
        ],
        wrong_reference=[r["task_id"] for r in references],
    )
    assert regenerated == committed


def test_the_empty_reference_list_agrees_with_both_runs_own_floors(committed) -> None:
    """Measured through the sandbox by `scripts/cascade.py`; checked here against the floor each
    run measured for itself, and against every reference row count either projection holds."""
    listed = {row["task_id"] for row in committed["rows_that_could_move"]["empty_reference"]}
    for name in ("cheap", "strong"):
        projection = _committed_side(committed, name).projection
        assert len(listed) == projection["execution_accuracy"]["empty_result_floor"]["tasks"]
        for task in projection["tasks"]:
            if task.get("reference_rows") is not None:
                assert (task["reference_rows"] == 0) == (task["task_id"] in listed)


def test_the_strong_side_is_3_6_frozen_and_the_cheap_side_is_the_always_cheap_run(committed):
    sources = committed["measurement"]["composed_from"]
    assert sources["strong"]["run_id"] == "20260910-024454-1f69bc"
    assert sources["strong"]["provider_and_model"] == ["groq/openai/gpt-oss-120b"]
    assert sources["cheap"]["run_id"] == "20260912-055938-9712c8"
    assert sources["cheap"]["provider_and_model"] == ["groq/openai/gpt-oss-20b"]
    assert sources["escalation"]["run_id"] == sources["cheap"]["run_id"]
    strong = next(p for p in committed["frontier"]["points"] if p["agent"] == "A1")
    assert (strong["solved"], strong["tokens"], strong["per_solved_task"]) == (116, 771_028, 6646.8)
    # 3.6's five retried tasks, attributed by task_id: 771,028 - 749,011.
    assert sum(v["tokens"] for v in sources["strong"]["earlier_attempts"].values()) == 22_017
    assert sources["strong"]["refused_requests"] == {"dev-0758": 1}


def test_the_cascade_lost_5_2s_metric_and_the_figures_are_the_ones_docs_results_quotes(
    committed,
) -> None:
    """A2 composed from run 20260912-055938-9712c8 (Groq, openai/gpt-oss-20b, 2026-09-12) and
    run 20260910-024454-1f69bc (Groq, openai/gpt-oss-120b, 2026-09-10). One more task than
    always-strong, for half as many tokens again: the verdict fixed in `29464c1` says it lost."""
    accuracy = committed["execution_accuracy"]
    assert (accuracy["solved"], accuracy["of"], accuracy["percent"]) == (117, 150, 78.0)
    tokens = committed["tokens"]
    assert tokens["by_model"] == {
        "groq/openai/gpt-oss-20b": 844_865,
        "groq/openai/gpt-oss-120b": 328_916,
    }
    assert (tokens["total"], tokens["per_solved_task"]) == (1_173_781, 10032.3)
    assert tokens["standing_trajectories_only"]["per_solved_task"] == 9889.0
    assert tokens["refused_requests"] == {"cheap": 33, "strong": 0}
    verdict = committed["frontier"]["verdict"]
    assert verdict["cascade_wins"] is False
    assert (verdict["cascade"], verdict["always_strong"]) == (10032.3, 6646.8)
    assert verdict["tasks_against_always_strong"] == 1
    assert verdict["token_ratio_against_always_strong"] == 1.5224
    assert committed["frontier"]["break_even_price_ratio"] == 0.5312
    points = {p["agent"]: p for p in committed["frontier"]["points"]}
    assert points["A2-cheap"]["dominated_by"] == ["A1"]
    assert points["A1"]["dominated_by"] == points["A2"]["dominated_by"] == []
    assert committed["escalation"]["escalated"] == 46
    assert committed["routes"] == {
        "rule_silent": {"tasks": 104, "cheap_solved": 89, "strong_solved": 88},
        "rule_fired": {"tasks": 46, "cheap_solved": 0, "strong_solved": 28},
    }
    assert committed["paired"]["against_always_strong"] == {
        "both": 113,
        "A2_only": 4,
        "A1_only": 3,
        "neither": 30,
    }
