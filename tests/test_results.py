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
