"""5.1's escalation rule: each clause, what it refuses, and the two committed files it wrote.

**No test here makes a live API call.** The rule is a pure function of three inputs, and the
run directories it reads are built here with the real writers. Two committed files are pinned:

- `docs/escalation-a1-working.json` — the rule on 3.6's strong run. Those transcripts are not
  committed, so the per-task inputs it holds are checked against the two frozen files they must
  agree with (`results/a1-working.json`, `results/a1-trajectory-metrics.json`), and the table is
  recomputed from them through :func:`decide`. It must reproduce the roadmap's 5.1 table.
- `docs/escalation-a2-cheap-smoke.json` — 5.1's preflight on the cheap model, regenerated here
  whole from the byte-for-byte lift in `tests/transcripts/a2-cheap-smoke-lifted/`.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from query_pilot.agents import MAX_OUTPUT_TOKENS, PROMPT_CEILING_CHARS
from query_pilot.agents.a1 import (
    ANSWER,
    BUDGET,
    PROMPT_CEILING,
    PROVIDER_REJECTED,
    TOOL_CALL_LIMIT_REACHED,
    TURN_LIMIT_REACHED,
)
from query_pilot.agents.escalation import (
    CLAUSES,
    COMPLETING,
    DEFINITIONS,
    DID_NOT_ANSWER,
    FIRST_QUERY_EMPTY,
    FIRST_QUERY_ERROR,
    NO_EXECUTE_SQL,
    VALIDATION_FAILED,
    Escalation,
    NotDecidable,
    TaskDecision,
    decide,
    document,
    read_decisions,
    tabulate,
)
from query_pilot.agents.metrics import EMPTY, ERROR, ROWS
from query_pilot.agents.transcript import TranscriptWriter
from query_pilot.agents.validate import MULTIPLE_STATEMENTS, NO_STATEMENT, NOT_A_QUERY
from query_pilot.client.types import Message, ToolCall

REPO = Path(__file__).resolve().parents[1]
A1_TABLE = REPO / "docs" / "escalation-a1-working.json"
A1_RESULTS = REPO / "results" / "a1-working.json"
A1_METRICS = REPO / "results" / "a1-trajectory-metrics.json"
PREFLIGHT = REPO / "docs" / "escalation-a2-cheap-smoke.json"
PREFLIGHT_LIFT = REPO / "tests" / "transcripts" / "a2-cheap-smoke-lifted"
TOOL_SIZES = REPO / "docs" / "a1-tool-sizes.json"

RUN_ID = "20260912-000000-bbbbbb"
STAMP = "2026-09-12T00:00:00+00:00"


# --- the rule, clause by clause ----------------------------------------------------------------


def test_the_four_clauses_are_the_roadmaps_in_its_order() -> None:
    assert CLAUSES == (DID_NOT_ANSWER, VALIDATION_FAILED, FIRST_QUERY_ERROR, FIRST_QUERY_EMPTY)
    assert NO_EXECUTE_SQL not in CLAUSES


def test_an_answered_valid_trajectory_whose_first_query_returned_rows_stays_cheap() -> None:
    decision = decide(termination=ANSWER, validation_rule=None, first_execute_sql=ROWS)
    assert decision == Escalation(()) and not decision.escalate


@pytest.mark.parametrize(
    "termination",
    [TURN_LIMIT_REACHED, TOOL_CALL_LIMIT_REACHED, PROMPT_CEILING, PROVIDER_REJECTED],
)
def test_clause_1_fires_on_every_complete_termination_but_answer(termination) -> None:
    decision = decide(termination=termination, validation_rule=None, first_execute_sql=ROWS)
    assert decision.clauses == (DID_NOT_ANSWER,)


@pytest.mark.parametrize("rule", [NO_STATEMENT, MULTIPLE_STATEMENTS, NOT_A_QUERY])
def test_clause_2_fires_on_any_validation_failure(rule) -> None:
    decision = decide(termination=ANSWER, validation_rule=rule, first_execute_sql=ROWS)
    assert decision.clauses == (VALIDATION_FAILED,)


def test_clause_3_fires_when_the_first_query_errored() -> None:
    decision = decide(termination=ANSWER, validation_rule=None, first_execute_sql=ERROR)
    assert decision.clauses == (FIRST_QUERY_ERROR,)


def test_clause_4_fires_when_the_first_query_returned_no_rows() -> None:
    decision = decide(termination=ANSWER, validation_rule=None, first_execute_sql=EMPTY)
    assert decision.clauses == (FIRST_QUERY_EMPTY,)


def test_running_no_query_at_all_is_not_a_trigger() -> None:
    """The rejected candidate. 21 of 24 such trajectories solved on 3.6's run."""
    decision = decide(termination=ANSWER, validation_rule=None, first_execute_sql=None)
    assert not decision.escalate


def test_every_clause_that_fires_is_reported_in_order() -> None:
    """The shape 3.6's tool_call_limit trajectories had: out of room, nothing said, and one of
    them had seen an empty first result."""
    decision = decide(
        termination=TOOL_CALL_LIMIT_REACHED, validation_rule=NO_STATEMENT, first_execute_sql=EMPTY
    )
    assert decision.clauses == (DID_NOT_ANSWER, VALIDATION_FAILED, FIRST_QUERY_EMPTY)


@pytest.mark.parametrize("termination", [BUDGET, "error", "", "answered"])
def test_a_trajectory_that_is_not_complete_is_refused_rather_than_decided(termination) -> None:
    assert BUDGET not in COMPLETING
    with pytest.raises(NotDecidable, match="retried by the run"):
        decide(termination=termination, validation_rule=None, first_execute_sql=ROWS)


def test_an_unknown_validation_rule_or_first_query_value_is_refused() -> None:
    with pytest.raises(NotDecidable, match="validation_rule"):
        decide(termination=ANSWER, validation_rule="not_sql", first_execute_sql=ROWS)
    with pytest.raises(NotDecidable, match="first_execute_sql"):
        decide(termination=ANSWER, validation_rule=None, first_execute_sql="zero")


def test_the_definitions_name_every_clause_and_the_rejected_candidate() -> None:
    for key in (*CLAUSES, NO_EXECUTE_SQL, "rule", "decidable", "frozen"):
        assert DEFINITIONS[key]
    assert PROVIDER_REJECTED in DEFINITIONS[DID_NOT_ANSWER]
    assert "NOT a clause" in DEFINITIONS[NO_EXECUTE_SQL]


# --- reading a run directory ---------------------------------------------------------------------


def _trajectory(directory: Path, task_id: str, *, outcome: str, first: str | None) -> None:
    """One bracket: an optional first `execute_sql` coming back as ``first``, then a reply."""
    writer = TranscriptWriter(directory, task_id)
    writer.start(
        run_id=RUN_ID, agent="A2-cheap", db_id="db", question="q", limits={}, started_at=STAMP
    )
    writer.message(Message(role="system", content="system"), turn=0)
    writer.message(Message(role="user", content="question"), turn=0)
    turn, calls = 1, 0
    if first is not None:
        call = ToolCall(id=f"{task_id}-{turn}", name="execute_sql", arguments={"sql": "SELECT 1"})
        writer.message(Message(role="assistant", content="", tool_calls=(call,)), turn=turn)
        writer.message(
            Message(role="tool", content="result", tool_call_id=call.id, name="execute_sql"),
            turn=turn,
        )
        writer.tool_result(
            turn=turn,
            call_id=call.id,
            name="execute_sql",
            ok=first != ERROR,
            error="no such column" if first == ERROR else None,
            rows_returned={ERROR: None, EMPTY: 0, ROWS: 3}[first],
        )
        turn, calls = 2, 1
    writer.message(Message(role="assistant", content="SELECT 1"), turn=turn)
    writer.end(outcome=outcome, turns=turn, tool_calls=calls, ended_at=STAMP)
    writer.close()


def _ledger(directory: Path, declared: list[str], rows: list[dict]) -> None:
    lines = [{"kind": "run_start", "run_id": RUN_ID, "declared": {"task_ids": declared}}]
    lines += [{"kind": "task", "run_id": RUN_ID, **row} for row in rows]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))


def _complete(task_id: str, termination: str, rule: str | None, solved: bool) -> dict:
    detail = {"termination": termination, "validation_rule": rule, "solved": solved}
    return {"task_id": task_id, "status": "complete", "detail": detail}


@pytest.fixture
def run_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "run"
    _trajectory(directory, "t-rows", outcome=ANSWER, first=ROWS)
    _trajectory(directory, "t-empty", outcome=ANSWER, first=EMPTY)
    _trajectory(directory, "t-error", outcome=ANSWER, first=ERROR)
    _trajectory(directory, "t-none", outcome=ANSWER, first=None)
    _trajectory(directory, "t-rejected", outcome=PROVIDER_REJECTED, first=None)
    # Retried: a first bracket that errored on its first query, then one that did not. The
    # last bracket stands (constraint 86).
    _trajectory(directory, "t-retried", outcome="error", first=ERROR)
    _trajectory(directory, "t-retried", outcome=ANSWER, first=ROWS)
    _trajectory(directory, "t-failed", outcome=BUDGET, first=None)
    _ledger(
        directory,
        [
            "t-rows",
            "t-empty",
            "t-error",
            "t-none",
            "t-rejected",
            "t-retried",
            "t-failed",
            "t-unrun",
        ],
        [
            _complete("t-rows", ANSWER, None, True),
            _complete("t-empty", ANSWER, None, False),
            _complete("t-error", ANSWER, None, True),
            _complete("t-none", ANSWER, None, True),
            _complete("t-rejected", PROVIDER_REJECTED, NO_STATEMENT, False),
            {"task_id": "t-retried", "status": "failed", "detail": {"exception": "X"}},
            _complete("t-retried", ANSWER, None, True),
            {"task_id": "t-failed", "status": "failed", "detail": {"exception": "BudgetStopped"}},
        ],
    )
    return directory


def test_every_complete_task_is_decided_and_every_other_declared_one_is_listed(
    run_directory,
) -> None:
    decisions = read_decisions(run_directory)
    assert decisions.run_id == RUN_ID
    clauses = {row.task_id: row.escalation.clauses for row in decisions.decided}
    assert clauses == {
        "t-rows": (),
        "t-empty": (FIRST_QUERY_EMPTY,),
        "t-error": (FIRST_QUERY_ERROR,),
        "t-none": (),
        "t-rejected": (DID_NOT_ANSWER, VALIDATION_FAILED),
        "t-retried": (),
    }
    assert decisions.undecided == ("t-failed", "t-unrun")


def test_a_ledger_and_transcript_that_disagree_on_termination_raise(run_directory) -> None:
    _ledger(run_directory, ["t-rows"], [_complete("t-rows", TURN_LIMIT_REACHED, None, False)])
    with pytest.raises(ValueError, match="disagree"):
        read_decisions(run_directory)


def test_a_complete_task_with_no_transcript_raises(tmp_path) -> None:
    _ledger(tmp_path, ["t-gone"], [_complete("t-gone", ANSWER, None, True)])
    with pytest.raises(ValueError, match="holds no closed trajectory"):
        read_decisions(tmp_path)


def test_the_table_counts_clauses_the_union_and_recall(run_directory) -> None:
    table = tabulate(read_decisions(run_directory).decided, outcomes=True)
    assert table["trajectories"] == 6
    assert table["would_escalate"] == {"count": 3, "solved": 1, "solve_rate": 0.333333}
    assert table["would_not_escalate"] == {"count": 3, "solved": 3, "solve_rate": 1.0}
    assert table["clauses"][FIRST_QUERY_ERROR] == {"count": 1, "solved": 1, "solve_rate": 1.0}
    assert table["fired_alone"] == {
        DID_NOT_ANSWER: 0,
        VALIDATION_FAILED: 0,
        FIRST_QUERY_ERROR: 1,
        FIRST_QUERY_EMPTY: 1,
    }
    assert table["not_a_clause"][NO_EXECUTE_SQL] == {"count": 2, "solved": 1, "solve_rate": 0.5}
    assert table["failures"] == {"count": 2, "escalated": 2, "recall": 1.0}


def test_the_preflight_form_computes_no_outcome_at_all(run_directory) -> None:
    table = tabulate(read_decisions(run_directory).decided, outcomes=False)
    assert "solved" not in json.dumps(table) and "failures" not in table
    assert table["clauses"][FIRST_QUERY_EMPTY] == {"count": 1}


def test_a_clause_that_never_fires_reports_tbd_rather_than_zero() -> None:
    row = TaskDecision("t", ANSWER, None, ROWS, True, Escalation(()))
    table = tabulate([row], outcomes=True)
    assert table["clauses"][FIRST_QUERY_ERROR]["solve_rate"] == "TBD"
    assert tabulate([], outcomes=True)["escalation_rate"] == "TBD"


# --- the rule on 3.6's run: the roadmap's 5.1 table, from committed data ------------------------


@pytest.fixture(scope="module")
def a1_table() -> dict:
    return json.loads(A1_TABLE.read_text())


def _decisions_from(payload: dict) -> list[TaskDecision]:
    return [
        TaskDecision(
            task_id=row["task_id"],
            termination=row["termination"],
            validation_rule=row["validation_rule"],
            first_execute_sql=row["first_execute_sql"],
            solved=row["solved"],
            escalation=decide(
                termination=row["termination"],
                validation_rule=row["validation_rule"],
                first_execute_sql=row["first_execute_sql"],
            ),
        )
        for row in payload["tasks"]
    ]


def test_the_committed_table_is_recomputed_from_its_own_inputs(a1_table) -> None:
    decisions = _decisions_from(a1_table)
    assert [list(d.escalation.clauses) for d in decisions] == [
        row["clauses"] for row in a1_table["tasks"]
    ]
    assert tabulate(decisions, outcomes=True) == a1_table["table"]
    assert a1_table["definitions"] == DEFINITIONS


def test_its_inputs_agree_with_the_two_frozen_files_of_3_6(a1_table) -> None:
    """The transcripts are not committed; what can be checked against committed data is."""
    frozen = {task["task_id"]: task for task in json.loads(A1_RESULTS.read_text())["tasks"]}
    assert {row["task_id"] for row in a1_table["tasks"]} == set(frozen)
    for row in a1_table["tasks"]:
        task = frozen[row["task_id"]]
        assert (row["termination"], row["validation_rule"], row["solved"]) == (
            task["termination"],
            task["validation_rule"],
            task["solved"],
        )
    metrics = json.loads(A1_METRICS.read_text())["recovery_rate"]["first_execute_sql"]
    counted = Counter(row["first_execute_sql"] or "none" for row in a1_table["tasks"])
    assert dict(counted) == metrics


def test_it_reproduces_the_roadmaps_5_1_table(a1_table) -> None:
    """Groq, openai/gpt-oss-120b, 2026-09-10, run 20260910-024454-1f69bc."""
    table = a1_table["table"]
    assert a1_table["run_id"] == "20260910-024454-1f69bc"
    assert a1_table["ledger"] == "runs/20260910-024454-1f69bc/ledger.jsonl"
    assert a1_table["requests"]["by_endpoint"] == {"groq openai/gpt-oss-120b": 827}
    assert table["trajectories"] == 150
    assert (table["would_escalate"]["count"], table["would_escalate"]["solved"]) == (10, 1)
    assert (table["would_not_escalate"]["count"], table["would_not_escalate"]["solved"]) == (
        140,
        115,
    )
    fired = {c: (table["clauses"][c]["count"], table["clauses"][c]["solved"]) for c in CLAUSES}
    assert fired == {
        DID_NOT_ANSWER: (8, 0),
        VALIDATION_FAILED: (8, 0),
        FIRST_QUERY_ERROR: (0, 0),
        FIRST_QUERY_EMPTY: (6, 1),
    }
    assert table["not_a_clause"][NO_EXECUTE_SQL] == {"count": 24, "solved": 21, "solve_rate": 0.875}
    # 26% recall: the cascade can only recover failures that announce themselves.
    assert table["failures"] == {"count": 34, "escalated": 9, "recall": 0.264706}


def test_the_ceiling_arithmetic_does_not_hold_on_a1s_own_requests(a1_table) -> None:
    """**The correction constraint 64 now carries.** The ceiling was derived with A0's 3.265
    characters per token; A1's own requests are denser, and the line fitted to them puts an
    attempt at the ceiling over the endpoint's per-minute budget. 3.6 never came near it."""
    sizes, derived = a1_table["attempt_sizes"], a1_table["ceiling_derivation"]
    assert derived["chars_per_prompt_token"] == 3.265
    assert derived["worst_case_attempt_tokens"] == derived["tpm"] == sizes["tpm"] == 8000
    assert sizes["chars_per_prompt_token"]["median"] < derived["chars_per_prompt_token"]
    fit = sizes["fit"]
    projected = fit["intercept_tokens"] + PROMPT_CEILING_CHARS / fit["chars_per_token"]
    assert abs(projected + MAX_OUTPUT_TOKENS - sizes["projected_attempt_at_ceiling"]) <= 2
    assert sizes["projected_attempt_at_ceiling"] > sizes["tpm"]
    # And nothing measured was near it.
    assert sizes["attempts_over_tpm"] == 0
    assert sizes["largest_attempt"]["tokens"] < sizes["tpm"] / 2
    assert sizes["longest_conversation"]["share_of_ceiling"] < 0.5


# --- the preflight on the cheap model, regenerated from its lift -------------------------------


def _ceiling_derivation() -> dict:
    """What `scripts/escalation_table.py` writes, rebuilt the same way from the same file."""
    budget = json.loads(TOOL_SIZES.read_text())["budget"]
    return {
        "chars_per_prompt_token": budget["chars_per_prompt_token"],
        "source": "docs/a1-tool-sizes.json, measured in docs/a0-prompt-sizes.json on A0's prompts",
        "worst_case_attempt_tokens": round(
            PROMPT_CEILING_CHARS / budget["chars_per_prompt_token"] + MAX_OUTPUT_TOKENS
        ),
        "tpm": budget["tpm"],
    }


@pytest.fixture(scope="module")
def preflight() -> dict:
    return json.loads(PREFLIGHT.read_text())


def test_the_preflight_file_is_regenerated_whole_from_the_lift(preflight) -> None:
    regenerated = document(
        PREFLIGHT_LIFT,
        outcomes=False,
        tpm=preflight["attempt_sizes"]["tpm"],
        ledger=preflight["ledger"],
        ceiling_derivation=_ceiling_derivation(),
    )
    assert regenerated == preflight


def test_the_preflight_names_no_outcome(preflight) -> None:
    assert preflight["outcomes_included"] is False
    assert "solved" not in json.dumps(preflight["table"])
    assert all("solved" not in row for row in preflight["tasks"])


def test_the_preflight_was_served_by_the_cheap_model_alone(preflight) -> None:
    """The role has no spillover, and every attempt row proves no request went elsewhere."""
    assert preflight["requests"]["by_endpoint"].keys() == {"groq openai/gpt-oss-20b"}
    assert preflight["attempt_sizes"]["served_by"].keys() == {"groq openai/gpt-oss-20b"}


def test_the_preflight_covers_the_smoke_set_and_decided_every_task(preflight) -> None:
    smoke = json.loads((REPO / "splits" / "smoke.json").read_text())
    ids = smoke["task_ids"] if isinstance(smoke, dict) else smoke
    assert sorted(row["task_id"] for row in preflight["tasks"]) == sorted(ids)
    assert preflight["tasks_declared"] == 15 and preflight["undecided"] == []


def test_the_preflight_tripped_neither_stop_condition(preflight) -> None:
    """Fixed with the author before it spent anything: no request over the endpoint's
    per-minute budget, and no trajectory stopped by the prompt ceiling."""
    assert preflight["attempt_sizes"]["attempts_over_tpm"] == 0
    assert PROMPT_CEILING not in preflight["terminations"]


# --- 5.2: the frozen rule applied, untuned, to the always-cheap run -----------------------------

WORKING = REPO / "docs" / "escalation-a2-cheap-working.json"
CHEAP_RESULTS = REPO / "results" / "a2-cheap-working.json"
CHEAP_METRICS = REPO / "results" / "a2-cheap-trajectory-metrics.json"


@pytest.fixture(scope="module")
def working() -> dict:
    return json.loads(WORKING.read_text())


def test_the_always_cheap_decisions_are_recomputed_from_their_own_inputs(working) -> None:
    """Every task's clauses re-decided through the frozen rule, and the table re-tabulated."""
    decisions = _decisions_from(working)
    assert [list(d.escalation.clauses) for d in decisions] == [
        row["clauses"] for row in working["tasks"]
    ]
    assert tabulate(decisions, outcomes=True) == working["table"]
    assert working["definitions"] == DEFINITIONS


def test_the_always_cheap_inputs_agree_with_the_runs_committed_files(working) -> None:
    """The transcripts are not committed; the projection and the metrics file are."""
    frozen = {task["task_id"]: task for task in json.loads(CHEAP_RESULTS.read_text())["tasks"]}
    assert {row["task_id"] for row in working["tasks"]} == set(frozen)
    for row in working["tasks"]:
        task = frozen[row["task_id"]]
        assert (row["termination"], row["validation_rule"], row["solved"]) == (
            task["termination"],
            task["validation_rule"],
            task["solved"],
        )
    metrics = json.loads(CHEAP_METRICS.read_text())["recovery_rate"]["first_execute_sql"]
    counted = Counter(row["first_execute_sql"] or "none" for row in working["tasks"])
    assert dict(counted) == metrics


def test_the_always_cheap_run_was_served_by_the_cheap_model_alone(working) -> None:
    assert working["run_id"] == "20260912-055938-9712c8"
    assert working["ledger"] == "runs/20260912-055938-9712c8/ledger.jsonl"
    assert working["requests"]["by_endpoint"].keys() == {"groq openai/gpt-oss-20b"}
    assert working["attempt_sizes"]["served_by"].keys() == {"groq openai/gpt-oss-20b"}
    assert working["tasks_declared"] == 150 and working["undecided"] == []
    assert working["outcomes_included"] is True


def test_the_always_cheap_run_tripped_neither_stop_condition(working) -> None:
    assert working["attempt_sizes"]["attempts_over_tpm"] == 0
    assert PROMPT_CEILING not in working["terminations"]


def test_the_rule_on_the_cheap_model_escalates_46_and_every_one_had_failed(working) -> None:
    """Groq, openai/gpt-oss-20b, 2026-09-12, run 20260912-055938-9712c8. Untuned: the rule is
    `c1f8520`'s. Its precision holds on the cheap model, and its recall is three times 3.6's."""
    table = working["table"]
    assert working["terminations"] == {"answer": 113, "provider_rejected": 28, "tool_call_limit": 9}
    assert table["trajectories"] == 150
    assert (table["would_escalate"]["count"], table["would_escalate"]["solved"]) == (46, 0)
    assert (table["would_not_escalate"]["count"], table["would_not_escalate"]["solved"]) == (
        104,
        89,
    )
    fired = {c: table["clauses"][c]["count"] for c in CLAUSES}
    assert fired == {
        DID_NOT_ANSWER: 37,
        VALIDATION_FAILED: 42,
        FIRST_QUERY_ERROR: 0,
        FIRST_QUERY_EMPTY: 8,
    }
    assert table["failures"] == {"count": 61, "escalated": 46, "recall": 0.754098}
    # The rejected candidate would have mattered on this model: 9 of 42 solved. Not tuned in.
    assert table["not_a_clause"][NO_EXECUTE_SQL] == {
        "count": 42,
        "solved": 9,
        "solve_rate": 0.214286,
    }
