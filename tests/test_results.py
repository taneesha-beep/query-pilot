"""The committed result files, checked against each other.

**No substrate, no keys, no network.** These read what 2.4 and 2.5 committed —
`results/a0-working.json` and `docs/failure-sample.json` — the way `tests/test_sandbox.py`
reads `docs/sandbox-caps.json`: the artifacts carry the measurement, and CI guards that the
things said about them stay true.

What this catches is a result file and the failures drawn from it drifting apart. The
projection is regenerable from its ledger and the sample is drawn from the projection's run,
so if a future session re-runs one and not the other, the two stop describing the same 150
tasks and nothing else would say so.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results" / "a0-working.json"
SAMPLE = REPO / "docs" / "failure-sample.json"

#: 2.5's protocol, fixed before the run of 2.4 started. Both numbers are the protocol's,
#: not the run's, and neither may be chosen after seeing a result.
SAMPLE_SIZE = 30
SEED = 20260908


def results() -> dict:
    return json.loads(RESULTS.read_text())


def sample() -> dict:
    return json.loads(SAMPLE.read_text())


def test_the_projection_carries_the_four_things_a_figure_needs_to_be_a_result():
    """Provider, model, date and ledger file. A number without those four is not one."""
    measurement = results()["measurement"]

    assert measurement["provider_and_model"] == ["groq/openai/gpt-oss-120b"]
    assert measurement["date"] == "2026-09-08"
    assert measurement["ledger"] == "runs/20260908-133316-faecd5/ledger.jsonl"
    assert measurement["run_id"] == "20260908-133316-faecd5"
    assert measurement["agent"] == "A0" and measurement["split"] == "working"


def test_the_accuracy_figure_is_the_solved_count_over_every_declared_task():
    """Not over the answered ones. A rate whose denominator moved is a different rate."""
    document = results()
    accuracy = document["execution_accuracy"]

    assert accuracy["of"] == document["measurement"]["tasks_declared"] == 150
    assert document["run"]["tasks_complete"] == 150 and document["run"]["tasks_failed"] == 0
    assert accuracy["solved"] == sum(1 for task in document["tasks"] if task.get("solved"))
    assert accuracy["percent"] == round(100 * accuracy["solved"] / accuracy["of"], 4)


def test_the_empty_result_floor_is_beside_the_accuracy_figure_and_not_below_it():
    """docs/EQUIVALENCE.md's requirement, which is easy to keep now and easy to lose later.

    The floor is what an agent scores by answering nothing, so an accuracy figure quoted
    without it overstates what the agent did. Asserting on the nesting rather than the prose
    is what makes it survive a rewrite: the floor cannot be moved out of the accuracy block
    without this failing.
    """
    accuracy = results()["execution_accuracy"]
    floor = accuracy["empty_result_floor"]

    assert set(floor) >= {"tasks", "of", "percent"}
    assert floor["of"] == accuracy["of"]
    assert floor["percent"] == round(100 * floor["tasks"] / floor["of"], 4)
    # And it has to be under the figure it qualifies, not a sibling somewhere else.
    assert "empty_result_floor" not in results()


def test_no_result_in_the_run_was_decided_by_a_cap_or_a_deadline():
    """The requirement the caps were derived against, checked against the run that used them."""
    sandbox = results()["sandbox"]

    assert sandbox["candidates_truncated"] == 0
    assert sandbox["candidates_timed_out"] == 0
    assert sandbox["completions_stopped_at_length"] == 0


def test_the_committed_file_carries_no_exception_message():
    """Diagnostic prose belongs in the gitignored ledger, never in a committed artifact."""
    assert '"message"' not in RESULTS.read_text()


def test_the_failure_sample_is_every_non_solve_of_the_run_it_names():
    """2.5's protocol: fewer than thirty means all of them, and the counts say so."""
    document, drawn = results(), sample()
    non_solves = [t["task_id"] for t in document["tasks"] if not t.get("solved")]

    assert drawn["run"]["run_id"] == document["measurement"]["run_id"]
    assert drawn["run"]["non_solves"] == len(non_solves)
    assert drawn["protocol"]["sample_size"] == SAMPLE_SIZE
    assert drawn["protocol"]["seed"] == SEED

    ids = [row["task_id"] for row in drawn["drawn"]]
    assert len(non_solves) <= SAMPLE_SIZE, "a larger population must be sampled, not read whole"
    assert sorted(ids) == sorted(non_solves)
    assert "all" in drawn["protocol"]["how_drawn"]


def test_no_solved_task_is_in_the_failure_sample():
    solved = {t["task_id"] for t in results()["tasks"] if t.get("solved")}

    assert not solved & {row["task_id"] for row in sample()["drawn"]}


def test_the_sample_says_which_tasks_had_been_seen_before_the_protocol_was_fixed():
    """They stay in the population. Naming them is what lets a reader discount them."""
    drawn = sample()
    seen = set(drawn["protocol"]["seen_before_the_protocol_was_fixed"])

    assert seen == {"dev-0126", "dev-0388", "dev-0489"}
    for row in drawn["drawn"]:
        assert row["seen_before"] is (row["task_id"] in seen)


def test_every_drawn_failure_carries_the_slug_the_ledger_gave_it():
    """The slug is read, never re-decided: 2.5 adds a taxonomy, it does not edit this one."""
    by_id = {t["task_id"]: t for t in results()["tasks"]}

    for row in sample()["drawn"]:
        assert row["reason"] == by_id[row["task_id"]]["reason"]
        assert row["db_id"] == by_id[row["task_id"]]["db_id"]


# --- A1's committed figures, and the comparison Phase 5 will be measured against ---------------

A1_RESULTS = REPO / "results" / "a1-working.json"
A1_METRICS = REPO / "results" / "a1-trajectory-metrics.json"


def a1() -> dict:
    return json.loads(A1_RESULTS.read_text())


def a1_metrics() -> dict:
    return json.loads(A1_METRICS.read_text())


def test_a1s_projection_carries_the_four_things_a_figure_needs_to_be_a_result():
    measurement = a1()["measurement"]
    assert measurement["provider_and_model"] == ["groq/openai/gpt-oss-120b"]
    assert measurement["date"] == "2026-09-10"
    assert measurement["ledger"] == "runs/20260910-024454-1f69bc/ledger.jsonl"
    assert measurement["run_id"] == "20260910-024454-1f69bc"
    assert measurement["agent"] == "A1" and measurement["split"] == "working"


def test_a1s_accuracy_is_a_rate_only_because_every_declared_task_has_an_outcome():
    """The refusal is the point: this run took six sessions and three tasks failed at a wall
    before being retried. A rate exists here only because the last of them came back."""
    document = a1()
    assert document["run"]["status"] == "complete"
    assert document["run"]["tasks_complete"] == 150 and document["run"]["tasks_failed"] == 0
    accuracy = document["execution_accuracy"]
    assert (accuracy["solved"], accuracy["of"], accuracy["percent"]) == (116, 150, 77.3333)
    assert len(document["tasks"]) == 150


def test_both_agents_were_measured_on_the_same_150_tasks():
    """The comparison is a statement about the loop only while this holds."""
    zero = {t["task_id"] for t in results()["tasks"]}
    one = {t["task_id"] for t in a1()["tasks"]}
    assert zero == one and len(zero) == 150
    assert results()["measurement"]["split"] == a1()["measurement"]["split"] == "working"
    # Same model, same role. Phase 5 is the only place the model varies.
    assert (
        results()["measurement"]["provider_and_model"] == a1()["measurement"]["provider_and_model"]
    )


def test_the_comparison_docs_results_commits_is_the_one_the_files_hold():
    """Every figure in the comparison table, recomputed from the two projections."""
    zero, one = results(), a1()
    assert zero["execution_accuracy"]["solved"] == 124
    assert one["execution_accuracy"]["solved"] == 116
    assert one["execution_accuracy"]["solved"] - zero["execution_accuracy"]["solved"] == -8
    # The difference is computed from the counts, not by subtracting the two rounded rates.
    # Those are stored at 4dp, and subtracting them gives -5.3334 -- a rounding artifact of
    # the subtraction, not the difference. The true gap is exactly 8/150.
    points = (
        100.0 * (one["execution_accuracy"]["solved"] - zero["execution_accuracy"]["solved"]) / 150
    )
    assert round(points, 4) == -5.3333
    assert (
        round(one["execution_accuracy"]["percent"] - zero["execution_accuracy"]["percent"], 4)
        == -5.3334
    )

    assert (zero["tokens"]["total"], one["tokens"]["total"]) == (106_740, 771_028)
    assert one["tokens"]["total"] - zero["tokens"]["total"] == 664_288
    assert round(one["tokens"]["total"] / zero["tokens"]["total"], 2) == 7.22
    assert (zero["tokens"]["per_solved_task"], one["tokens"]["per_solved_task"]) == (860.8, 6646.8)
    assert (zero["run"]["attempts"], one["run"]["attempts"]) == (150, 827)

    # There is no cost per ADDITIONAL solved task, because there are no additional solves.
    assert one["execution_accuracy"]["solved"] < zero["execution_accuracy"]["solved"]


def test_the_empty_result_floor_is_the_same_measurement_beside_both_figures():
    """Measured independently in each run through the same sandbox, so it agreeing is a check
    on the split rather than a copied constant."""
    for document in (results(), a1()):
        floor = document["execution_accuracy"]["empty_result_floor"]
        assert (floor["tasks"], floor["of"], floor["percent"]) == (7, 150, 4.6667)


def test_every_a1_no_sql_is_a_tool_call_limit_trajectory_that_wrote_nothing():
    """`no_sql` means two different things for A1, and `validation_rule` is what tells them
    apart -- there is deliberately no ninth equivalence slug. All eight are one shape."""
    document = a1()
    assert document["reasons"]["no_sql"] == 8
    assert document["validation_rules"] == {"no_statement": 8}
    assert document["terminations"] == {"answer": 142, "tool_call_limit": 8}
    no_sql = [t for t in document["tasks"] if t.get("reason") == "no_sql"]
    assert len(no_sql) == 8
    assert all(t["termination"] == "tool_call_limit" for t in no_sql)
    assert all(t["validation_rule"] == "no_statement" for t in no_sql)
    # A0 produced none at all, which is what makes this A1's own failure mode.
    assert results()["reasons"].get("no_sql", 0) == 0


def test_a1_was_measured_under_the_limits_it_was_declared_with():
    """Eight tasks were lost to the tool-call limit. Raising it after seeing that would be
    choosing the number, so every trajectory records what it ran under."""
    tasks = a1()["tasks"]
    assert {t["turn_limit"] for t in tasks} == {14}
    assert {t["tool_call_limit"] for t in tasks} == {12}


def test_recovery_rates_headline_denominator_is_empty_and_says_tbd_rather_than_zero():
    """**A rate over nothing is not zero.** Not one execute_sql in 150 trajectories errored,
    so the number 3.4 exists to produce reports TBD -- and the two counts beside it are what
    keep that honest rather than silent."""
    recovery = a1_metrics()["recovery_rate"]
    assert recovery["error"] == {"denominator": 0, "recovered": 0, "rate": "TBD"}
    assert recovery["trajectories_with_any_execute_sql_error"] == 0
    # `empty` is reported apart because an empty result can be the correct answer here.
    assert recovery["empty"]["denominator"] == 6 and recovery["empty"]["recovered"] == 1
    assert recovery["error_or_empty"]["denominator"] == 6
    assert recovery["tasks_with_no_execute_sql"] == 24
    assert recovery["first_execute_sql"] == {"rows": 120, "none": 24, "empty": 6}
    assert sum(recovery["first_execute_sql"].values()) == 150


def test_the_metrics_and_the_projection_describe_the_same_run():
    """Two committed files carrying overlapping facts agree only while they come from one run
    directory. This is what stops a future session regenerating one and not the other."""
    metrics, document = a1_metrics(), a1()
    assert metrics["run_id"] == document["measurement"]["run_id"]
    assert metrics["tasks"]["transcripts_read"] == document["measurement"]["tasks_declared"]
    assert metrics["tasks"]["with_outcome"] == document["run"]["tasks_complete"] == 150
    assert metrics["tasks"]["solved"] == document["execution_accuracy"]["solved"] == 116
    assert metrics["tasks"]["terminations"] == document["terminations"]
    # The transcript's own checksum agreed with its events in every one of the 150.
    assert metrics["tasks"]["counts_disagreeing_with_end"] == 0
    assert metrics["tasks"]["incomplete_transcripts"] == 0


def test_the_trajectory_metrics_are_the_ones_docs_results_quotes():
    metrics = a1_metrics()
    calls, turns = metrics["tool_calls_per_task"], metrics["turns_to_solve"]
    assert (calls["n"], calls["mean"], calls["median"], calls["p90"]) == (150, 4.3467, 4.0, 7)
    assert (turns["n"], turns["mean"], turns["median"], turns["p90"]) == (116, 4.819, 5.0, 6)
    waste = metrics["wasted_calls"]
    assert (waste["wasted"], waste["calls_counted"], waste["rate"]) == (17, 556, 0.030576)
    # The two exclusions are counted rather than folded in.
    assert waste["tasks_counted"] == 142 and waste["tasks_without_final_sql"] == 8
    assert metrics["repairs"] == {"attempts": 1, "successes": 1, "blocked": {}}


def test_every_definition_travels_with_a1s_published_numbers():
    """3.6 publishes these, so 3.4's definitions are frozen from here (constraint 59). A file
    that did not carry them would be four numbers nobody could check."""
    definitions = a1_metrics()["definitions"]
    assert set(definitions) >= {
        "tool_calls_per_task",
        "turns_to_solve",
        "recovery_rate",
        "wasted_calls",
    }
    assert "tool_result events" in definitions["tool_calls_per_task"]
    assert "three denominators, never one" in definitions["recovery_rate"]
    assert "APPROXIMATE" in definitions["wasted_calls"]
