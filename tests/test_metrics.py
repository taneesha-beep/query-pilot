"""3.4's metrics, against committed transcripts. **The definitions are what is tested.**

Two fixture directories and they do different jobs, which is why there are two. `built/` is
hand-built and every expected value below is exact — that is the specification, and a metric
whose definition drifts fails here rather than in 3.6's published table. `lifted/` is a real
run, byte for byte, and it proves the reader parses what the writer actually emits rather
than what the reader's author imagined it emits. `tests/transcripts/README.md` says which is
which, file by file.

**No number computed here is a result.** `lifted/` is an acceptance run on the smoke set; 3.6
is A1's measured run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from query_pilot.agents.metrics import (
    EMPTY,
    ERROR,
    ROWS,
    TBD,
    compute,
    contributed,
    normalise_sql,
    percentile,
    write_metrics,
)

REPO = Path(__file__).resolve().parents[1]
BUILT = REPO / "tests" / "transcripts" / "built"
LIFTED = REPO / "tests" / "transcripts" / "lifted"


@pytest.fixture(scope="module")
def built() -> dict:
    return compute(BUILT).as_json()


@pytest.fixture(scope="module")
def by_task() -> dict:
    return {row.task_id: row for row in compute(BUILT).tasks}


# --- percentile and median, whose rules are stated rather than borrowed --------------------


def test_percentile_is_nearest_rank_and_returns_a_value_that_happened() -> None:
    """No interpolation. These are counts of tool calls, and an interpolated p90 of 4.3 is a
    number no trajectory made — a reader comparing it against `TOOL_CALL_LIMIT` would be
    comparing against a value the loop cannot produce.
    """
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(values, 0.90) == 9
    assert percentile(values, 0.50) == 5
    # Every result is one of the inputs, at every fraction.
    assert all(percentile(values, f / 100) in values for f in range(1, 101))


def test_percentile_of_one_value_is_that_value() -> None:
    assert percentile([7], 0.90) == 7


def test_percentile_of_nothing_is_refused_rather_than_invented() -> None:
    with pytest.raises(ValueError):
        percentile([], 0.90)


# --- tool calls per task ------------------------------------------------------------------


def test_unexecuted_tool_calls_are_not_counted(by_task) -> None:
    """**The one that would be wrong by default.** A trajectory stopped at the turn limit
    keeps an assistant message whose calls were never executed, because there was no turn
    left to feed them back into. `t-turn-limit` asks for six and executes four.
    """
    row = by_task["t-turn-limit"]
    assert row.tool_calls == 4
    assert row.termination == "turn_limit"

    # The `end` event carries a `tool_calls` count, so only the messages are read here --
    # which is the same distinction the metric itself makes, one layer down.
    asked = sum(
        len(event["tool_calls"])
        for line in (BUILT / "transcripts" / "t-turn-limit.jsonl").read_text().splitlines()
        if line.strip()
        for event in [json.loads(line)]
        if event["kind"] == "message"
    )
    assert asked == 6


def test_tool_calls_per_task_reports_a_count_beside_the_summary(built) -> None:
    """A p90 over ten values is arithmetic rather than a statistic, and a reader has to be
    able to see which one they are looking at.
    """
    assert built["tool_calls_per_task"] == {
        "n": 10,
        "mean": 2.3,
        "median": 2.0,
        "p90": 4,
        "min": 1,
        "max": 6,
    }


# --- turns to solve -----------------------------------------------------------------------


def test_turns_to_solve_covers_solved_tasks_only(built) -> None:
    """Six of the ten solved. The other four are not zeros in this distribution — they are
    not in it, which is what "over solved tasks only" means.
    """
    assert built["tasks"]["solved"] == 6
    assert built["turns_to_solve"]["n"] == 6
    assert built["turns_to_solve"]["distribution"] == {"2": 1, "3": 3, "4": 1, "7": 1}


def test_a_repair_turn_counts_as_a_turn(by_task) -> None:
    """It is a request the run paid for and it has its own `AttemptRow`. `t-repaired` makes
    one tool call across three turns, the third of which is the repair.
    """
    row = by_task["t-repaired"]
    assert row.turns == 3
    assert row.repair_attempts == 1 and row.repair_succeeded


# --- recovery rate, and its three denominators --------------------------------------------


def test_recovery_rate_is_three_numbers_with_their_denominators(built) -> None:
    """**The single most interesting figure in the phase, and the one most easily inflated.**

    `error` is the headline: the model wrote SQL that did not run, saw the error, and solved.
    `empty` is apart because an empty result can be the correct answer — 49 of the frame's
    1,034 references return no rows, priced at 4.7389% in docs/EQUIVALENCE.md — so that
    denominator holds tasks with nothing to recover from. Pooling them would inflate the
    headline, and 4.3 quotes two more denominators right beside these.
    """
    recovery = built["recovery_rate"]
    assert recovery["error"] == {"denominator": 2, "recovered": 1, "rate": 0.5}
    assert recovery["empty"] == {"denominator": 1, "recovered": 1, "rate": 1.0}
    assert recovery["error_or_empty"] == {"denominator": 3, "recovered": 2, "rate": 0.666667}


def test_a_task_with_no_execute_sql_is_in_no_denominator(built, by_task) -> None:
    """It has no *first* `execute_sql`, so it is not eligible — counted apart rather than
    counted as a failure to recover, which would put four tasks into a rate about two.
    """
    assert by_task["t-no-execute"].first_execute_sql is None
    assert built["recovery_rate"]["tasks_with_no_execute_sql"] == 3
    assert built["recovery_rate"]["error"]["denominator"] == 2


def test_a_task_the_run_failed_is_in_no_denominator(by_task) -> None:
    """`solved` is absent rather than false: the model never got to be wrong, and a false
    would put an infrastructure failure into a rate about the model. Same rule as 2.4's.
    """
    assert by_task["t-failed"].solved is None


@pytest.mark.parametrize(
    ("task_id", "kind"),
    [
        ("t-recovers", ERROR),
        ("t-no-recovery", ERROR),
        ("t-empty-first", EMPTY),
        ("t-repaired", ROWS),
        ("t-no-execute", None),
    ],
)
def test_the_first_execute_sql_is_classified_by_how_it_came_back(by_task, task_id, kind) -> None:
    assert by_task[task_id].first_execute_sql == kind


def test_any_error_and_any_empty_are_counted_beside_the_rates(built) -> None:
    """Counts, not a fourth rate. The smoke run showed the difference is real — one
    trajectory had two `execute_sql` return no rows while its first returned one — and a
    fourth rate would be a fourth denominator in a table that already carries three.
    """
    recovery = built["recovery_rate"]
    assert recovery["trajectories_with_any_execute_sql_error"] == 2
    assert recovery["trajectories_with_any_execute_sql_empty"] == 1


def test_a_rate_over_no_denominator_is_tbd_rather_than_zero() -> None:
    """**A rate over nothing is not zero**, and writing 0.0 is how a denominator vanishes
    from a table that quotes it. This is the project's `TBD` convention, and the lifted run
    is a real case of it: none of its five first queries errored.
    """
    lifted = compute(LIFTED).as_json()
    assert lifted["recovery_rate"]["error"]["denominator"] == 0
    assert lifted["recovery_rate"]["error"]["rate"] == TBD


# --- wasted calls, and where the definition is approximate --------------------------------


def test_wasted_calls_are_the_ones_after_the_last_contributing_call(by_task) -> None:
    """`t-wasteful` runs the final query at call 3 and then makes three more."""
    assert by_task["t-wasteful"].tool_calls == 6
    assert by_task["t-wasteful"].wasted_calls == 3


def test_the_approximation_is_real_and_this_is_it() -> None:
    """**Stated in a test rather than only in a docstring**, because it is the caveat that
    has to travel beside the number.

    Describing a table the final query never names is counted wasted — even though that call
    may have been exactly what ruled the table out. The metric cannot see the difference, so
    it is an upper bound on waste rather than a measurement of it.
    """
    final = "SELECT name FROM singer"
    assert contributed("describe_table", {"table": "singer"}, final)
    assert not contributed("describe_table", {"table": "stadium"}, final)


def test_list_tables_contributes_whenever_the_final_query_names_a_table() -> None:
    """It made every table name knowable, and the probe has it called first in 3 of 3."""
    assert contributed("list_tables", {}, "SELECT name FROM singer")
    # ...but not when nothing was discovered and nothing could have helped.
    assert not contributed("list_tables", {}, "SELECT 1")


def test_an_execute_sql_contributes_only_when_it_ran_the_final_query() -> None:
    final = "SELECT name FROM singer ORDER BY age DESC"
    assert contributed("execute_sql", {"sql": "select  NAME from Singer order by AGE desc;"}, final)
    assert not contributed("execute_sql", {"sql": "SELECT age FROM singer"}, final)


def test_a_table_named_inside_a_literal_is_not_a_match() -> None:
    """The identifiers are read with literals and comments blanked, for the reason
    `blank_literals` was made public: a name matched inside a string is a match that is not
    there, and it would silently mark a wasted call as contributing.
    """
    assert not contributed("describe_table", {"table": "stadium"}, "SELECT 'stadium' FROM singer")


def test_normalise_sql_is_not_an_equivalence_rule() -> None:
    """It asks whether the model ran the text it later gave — a question about a trajectory.
    Whether two queries give the same answer is `equivalence.compare`'s, decided by running
    them, and it is the only thing in this project allowed to say two queries agree.
    """
    assert normalise_sql("SELECT  a\n FROM t ;") == normalise_sql("select a from t")
    assert normalise_sql("SELECT a FROM t") != normalise_sql("SELECT a FROM t WHERE 1")


def test_the_two_exclusions_are_counted_rather_than_folded_in(built, by_task) -> None:
    """A task with no final SQL has nothing to have contributed to, and one with no tool
    calls has nothing to waste. Counting either as zero waste would report a clean
    trajectory where there was no trajectory to be clean.
    """
    assert by_task["t-turn-limit"].final_sql is None
    assert by_task["t-turn-limit"].wasted_calls is None
    assert built["wasted_calls"]["tasks_without_final_sql"] == 2
    assert built["wasted_calls"]["tasks_counted"] == 8


def test_the_wasted_call_rate_is_pooled_and_per_task_and_both_are_reported(built) -> None:
    """They differ, and which one a table quotes changes what it says. 4 wasted of 18 calls
    counted is 22.2%; the mean per task is half a call.
    """
    assert built["wasted_calls"]["wasted"] == 4
    assert built["wasted_calls"]["calls_counted"] == 18
    assert built["wasted_calls"]["rate"] == 0.222222
    assert built["wasted_calls"]["per_task_mean"] == 0.5


# --- the transcript's own edges -----------------------------------------------------------


def test_a_retried_task_keeps_its_last_trajectory(by_task) -> None:
    """The same rule the ledger's rows follow: the earlier attempt is evidence, not a second
    measurement. `t-retried`'s first bracket stopped on `budget`; its second answered.
    """
    row = by_task["t-retried"]
    assert row.termination == "answer"
    assert row.turns == 3 and row.tool_calls == 2


def test_a_transcript_with_no_end_event_reads_as_incomplete(built, by_task) -> None:
    """A process killed outright does not record its own death, and the count of those is
    reported rather than left to look like a short trajectory.
    """
    assert by_task["t-truncated"].complete is False
    assert built["tasks"]["incomplete_transcripts"] == 1


def test_the_end_events_counts_are_checked_against_the_events(built) -> None:
    """The `end` counts are a checksum, not a source — this is the check that makes that
    true. It caught a defect in these very fixtures: the retried task's second bracket had
    been written restarting `seq` at 1, which `TranscriptWriter` does not do.
    """
    assert built["tasks"]["counts_disagreeing_with_end"] == 0


# --- the lifted run: the reader against output nobody designed it for ---------------------


def test_the_lifted_run_parses_and_reports_what_it_actually_did() -> None:
    """Byte for byte what run `20260908-155422-c28177` wrote on 2026-09-08. **Not a result.**"""
    lifted = compute(LIFTED).as_json()
    assert lifted["run_id"] == "20260908-155422-c28177"
    assert lifted["tasks"]["transcripts_read"] == 5
    assert lifted["tasks"]["with_outcome"] == 5
    assert lifted["tasks"]["incomplete_transcripts"] == 0
    assert lifted["tasks"]["counts_disagreeing_with_end"] == 0
    assert lifted["tasks"]["terminations"] == {"answer": 4, "tool_call_limit": 1}
    assert lifted["repairs"] == {"attempts": 1, "successes": 1, "blocked": {}}


def test_the_final_sql_read_from_a_lifted_transcript_matches_the_ledgers_own() -> None:
    """**The join checked rather than assumed.** The wasted-call rate has to be computable
    from the transcript alone, so the final query is read out of the last assistant message
    with the same validator the agent used — and it must agree with what the agent wrote into
    the ledger's `detail`, or one of the two is lying.
    """
    ledger = {
        row["task_id"]: (row.get("detail") or {}).get("sql")
        for line in (LIFTED / "ledger.jsonl").read_text().splitlines()
        if line.strip()
        for row in [json.loads(line)]
        if row.get("kind") == "task"
    }
    for row in compute(LIFTED).tasks:
        assert row.final_sql == ledger[row.task_id], row.task_id


def test_the_live_repair_is_visible_in_the_lifted_transcript() -> None:
    """`dev-0317`: the model ran the query, was shown `20`, and replied `20` — the count
    rather than the statement. 3.3 rejected it, fed the error back, and the second reply was
    the query. This is the case the narrower reading of the repair rule would have scored
    `no_sql`.
    """
    row = next(r for r in compute(LIFTED).tasks if r.task_id == "dev-0317")
    assert row.repair_attempts == 1 and row.repair_succeeded
    assert row.turns == 3 + 1
    assert row.solved is True


# --- the script's output ------------------------------------------------------------------


def test_every_definition_travels_with_the_numbers(built, tmp_path) -> None:
    """A committed metrics file whose denominators were only in the source would be four
    numbers nobody could check. 3.6 publishes these and 4.3 quotes a rate beside them.
    """
    for key in ("tool_calls_per_task", "turns_to_solve", "recovery_rate", "wasted_calls"):
        assert key in built["definitions"]
    assert "upper bound on waste" in built["definitions"]["wasted_calls"]
    assert "nearest rank" in built["definitions"]["percentile"]

    written = tmp_path / "out" / "metrics.json"
    payload = write_metrics(BUILT, written)
    assert json.loads(written.read_text()) == payload
