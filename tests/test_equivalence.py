"""The measurement instrument, tested rule by rule.

Every accuracy figure this project reports is `query_pilot.equivalence`'s opinion, so each
of the seven decisions in `docs/EQUIVALENCE.md` has a case here naming what it admits and
what it rejects. Where a decision could plausibly have gone the other way, the test says so
in its name, because the point of writing this before generating a query is that the choice
stays visible afterwards.

Nothing here touches a database. The rule compares rows.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from query_pilot.equivalence import (
    ABSOLUTE_TOLERANCE,
    RELATIVE_TOLERANCE,
    Comparison,
    compare,
    orders_rows,
    values_equal,
)


def solved(reference, candidate, **kwargs) -> Comparison:
    return compare(reference, candidate, ordered=kwargs.pop("ordered", False), **kwargs)


# --- row order ----------------------------------------------------------------------------


def test_rows_match_in_any_order_when_the_reference_did_not_ask_for_one():
    """`SELECT name FROM singer` — the question fixes no order, so neither does the rule."""
    reference = [("Joe",), ("Rose",), ("Tribal King",)]
    assert solved(reference, [("Tribal King",), ("Joe",), ("Rose",)]).solved


def test_rows_must_match_in_order_when_the_reference_says_order_by():
    """`SELECT name FROM singer ORDER BY age DESC` — now the sequence is the answer."""
    reference = [("Joe",), ("Rose",), ("Tribal King",)]
    shuffled = [("Rose",), ("Joe",), ("Tribal King",)]
    assert compare(reference, reference, ordered=True).solved
    verdict = compare(reference, shuffled, ordered=True)
    assert not verdict.solved
    assert verdict.reason == "value_mismatch"
    assert "row 0" in verdict.detail


@pytest.mark.parametrize(
    ("sql", "ordered"),
    [
        ("SELECT name FROM singer", False),
        ("SELECT name FROM singer ORDER BY age DESC", True),
        ("select name from singer order   by age", True),
        ("SELECT name FROM singer ORDER BY age DESC LIMIT 1", True),
        # A subquery's ORDER BY orders the subquery. The caller sees an unordered result.
        ("SELECT name FROM (SELECT name FROM singer ORDER BY age)", False),
        ("SELECT a FROM t WHERE x IN (SELECT y FROM z ORDER BY w)", False),
        # ...but one *after* a subquery, at depth zero, is the outer query's own.
        ("SELECT a FROM t WHERE x IN (SELECT y FROM z ORDER BY w) ORDER BY a", True),
        # A compound query's trailing ORDER BY orders the whole thing.
        ("SELECT a FROM t UNION SELECT b FROM u ORDER BY 1", True),
        # Not an ORDER BY: it is inside a string, a quoted identifier, and a comment.
        ("SELECT 'order by age' FROM t", False),
        ('SELECT "order by" FROM t', False),
        ("SELECT a FROM t -- order by age", False),
        ("SELECT a FROM t /* order by age */", False),
    ],
)
def test_only_a_top_level_order_by_fixes_the_row_order(sql, ordered):
    assert orders_rows(sql) is ordered


# --- column order -------------------------------------------------------------------------


def test_columns_are_compared_by_position_and_never_by_name():
    """Names are ignored, because SQLite names an unaliased expression with its own text.

    `count(*)`, `COUNT(*)` and `n` are three names for one answer, and matching on them
    would reject nearly every aggregate a model writes. The cost is on this line: the same
    values in a different column order is a non-solve.
    """
    reference = [("Joe", 30)]
    assert solved(reference, [("Joe", 30)]).solved
    swapped = solved(reference, [(30, "Joe")])
    assert not swapped.solved
    assert swapped.reason == "value_mismatch"


def test_a_different_number_of_columns_is_its_own_refusal():
    verdict = solved([("Joe", 30)], [("Joe", 30, "France")])
    assert not verdict.solved
    assert verdict.reason == "column_count"
    assert "3 columns against the reference's 2" in verdict.detail


# --- duplicate rows -----------------------------------------------------------------------


def test_duplicate_rows_are_counted_which_is_multiset_and_not_set():
    """The choice that decides what a stray DISTINCT costs.

    Under set comparison these two would be the same answer. They are not: one query says
    three singers are French and the other says France appears at all.
    """
    reference = [("France",), ("France",), ("France",)]
    verdict = solved(reference, [("France",)])
    assert not verdict.solved
    assert verdict.reason == "row_count"
    assert solved(reference, [("France",), ("France",), ("France",)]).solved


def test_a_group_by_that_collapses_rows_the_reference_keeps_is_a_non_solve():
    # Named because it is the common way a candidate loses rows, and because multiset
    # comparison is what makes it a loss rather than a rounding.
    reference = [("pop", 3), ("rock", 3), ("jazz", 3)]
    collapsed = [("pop", 3), ("rock", 3)]
    assert solved(reference, collapsed).reason == "row_count"


# --- NULL ---------------------------------------------------------------------------------


def test_null_equals_null_which_sql_denies_and_a_result_set_needs():
    assert values_equal(None, None)
    assert solved([("Joe", None)], [("Joe", None)]).solved


@pytest.mark.parametrize("other", [0, 0.0, "", "NULL", "None", [], False])
def test_null_equals_nothing_else_however_empty_it_looks(other):
    """Coercing NULL to a falsy value hides the most common wrong-join failure there is.

    A LEFT join where the reference wrote an INNER one produces exactly this: a NULL where
    the reference has a value, or a NULL row where the reference has none.
    """
    assert not values_equal(None, other)
    assert not values_equal(other, None)


def test_a_left_join_filling_nulls_where_the_reference_has_none_is_caught():
    reference = [("Joe", 30), ("Rose", 41)]
    with_nulls = [("Joe", 30), ("Rose", None)]
    verdict = solved(reference, with_nulls)
    assert not verdict.solved
    assert verdict.reason == "value_mismatch"


# --- floats -------------------------------------------------------------------------------


def test_the_float_tolerance_admits_a_reformulated_aggregate():
    """What the tolerance is for: `SUM(x)/COUNT(x)` and `AVG(x)` summing in another order.

    Both derived from the same measured ceiling. The largest table in this substrate is
    510,437 rows, so worst-case accumulated drift is about 1.13e-10 relative — inside the
    tolerance by a factor of nearly nine.
    """
    assert values_equal(41.333333333333336, 41.33333333333333)
    assert values_equal(1.0, 1.0 + 5e-10)
    assert solved([("avg", 41.333333333333336)], [("avg", 41.33333333333333)]).solved


def test_the_float_tolerance_rejects_a_difference_that_could_mean_something():
    """The other half of the pair. A millionth is not a rounding, it is a wrong answer."""
    assert not values_equal(41.333333, 41.334)
    assert not values_equal(1.0, 1.000001)
    verdict = solved([("avg", 41.333333)], [("avg", 41.334)])
    assert not verdict.solved
    assert verdict.reason == "value_mismatch"


def test_the_tolerance_has_both_halves_and_each_one_does_something():
    # Relative, which absolute alone would reject at large magnitudes.
    assert values_equal(1e9, 1e9 + 0.5)
    assert not values_equal(1e9, 1e9 + 5.0)
    # Absolute, which relative alone collapses to zero for at and around zero.
    assert values_equal(0.0, 1e-10)
    assert not values_equal(0.0, 1e-8)
    assert (ABSOLUTE_TOLERANCE, RELATIVE_TOLERANCE) == (1e-9, 1e-9)


def test_an_integer_and_a_real_of_the_same_value_are_the_same_answer():
    # Which of the two SQLite hands back depends on how the query was written, not on what
    # the answer is.
    assert values_equal(2, 2.0)
    assert solved([("total", 2)], [("total", 2.0)]).solved


def test_text_never_coerces_to_a_number_however_numeric_it_looks():
    """The strict direction, chosen so a wrong column type stays visible.

    Allowing it would admit '0.50' as 0.5 too, and the cost of that is a comparison that
    quietly forgives the kind of mistake this project is trying to count.
    """
    assert not values_equal("5", 5)
    assert not values_equal(5, "5")
    assert not values_equal("0.50", 0.5)
    assert solved([("id", "5")], [("id", "5")]).solved


def test_two_infinities_are_equal_and_tolerance_cannot_be_asked_to_prove_it():
    # `inf - inf` is NaN and no comparison against a NaN is true, so the tolerance path
    # would answer False for two identical values.
    assert values_equal(float("inf"), float("inf"))
    assert not values_equal(float("inf"), float("-inf"))
    assert not values_equal(float("inf"), 1e308)
    assert values_equal(float("nan"), float("nan"))


# --- empty results ------------------------------------------------------------------------


def test_an_empty_result_matching_an_empty_reference_is_a_solve():
    """Stated explicitly because it is the one case where doing nothing scores.

    49 of the 1,034 reference queries in this frame return no rows, so a query returning
    nothing solves 4.74% of them. That floor is in `docs/EQUIVALENCE.md` so no reading of
    an accuracy number forgets it.
    """
    verdict = solved([], [])
    assert verdict.solved
    assert verdict.reason == "solved"
    assert verdict.detail == "both results are empty"


def test_empty_against_rows_and_rows_against_empty_are_both_refused():
    assert solved([("Joe",)], []).reason == "row_count"
    assert solved([], [("Joe",)]).reason == "row_count"


# --- errors -------------------------------------------------------------------------------


def test_an_error_is_a_non_solve_and_never_a_skip():
    """It stays in the denominator. A query that does not run did not answer."""
    verdict = solved([("Joe",)], None, candidate_error="no such column: singer.nmae")
    assert not verdict.solved
    assert verdict.reason == "candidate_error"
    assert "no such column" in verdict.detail


def test_a_reference_that_fails_is_not_filed_as_the_candidates_failure():
    # Should not arise — the frame is every task whose reference query executes — but if it
    # ever does it is a fact about the substrate, not about the model.
    verdict = solved(None, [("Joe",)], reference_error="database is locked")
    assert verdict.reason == "reference_error"
    assert not verdict.solved


def test_missing_rows_with_no_error_given_is_still_not_a_skip():
    assert solved(None, [("Joe",)]).solved is False
    assert solved([("Joe",)], None).solved is False


# --- truncation ---------------------------------------------------------------------------


def test_a_truncated_result_is_its_own_reason_and_not_a_wrong_answer():
    """2.2 adds a row cap and a byte cap, and this is what the rule does when one fires.

    Folding it into `row_count` would attribute a cap's effect to the model. Named
    separately, 2.4 can count how many tasks the cap decided, and a non-zero count is an
    argument for raising the cap rather than for a lower accuracy figure.
    """
    reference = [(n,) for n in range(5)]
    capped = solved(reference, [(n,) for n in range(3)], candidate_truncated=True)
    assert not capped.solved
    assert capped.reason == "truncated"
    assert "not comparable" in capped.detail

    # Even when the rows happen to agree, a capped result is not evidence that they do.
    assert solved(reference, reference, candidate_truncated=True).reason == "truncated"
    assert solved(reference, reference, reference_truncated=True).reason == "truncated"
    assert (
        "both results"
        in solved(reference, reference, reference_truncated=True, candidate_truncated=True).detail
    )


# --- what the rule returns ------------------------------------------------------------------


def test_the_result_carries_why_and_is_shaped_for_the_ledger():
    """Not a bool: 2.5 reads thirty failures by hand and Phase 4 counts them by kind.

    `as_detail()` is what 2.4 drops into a task row's free-form `detail` mapping, which
    nothing in `run/` looks inside — the seam that keeps the run package from ever learning
    what a solve is.
    """
    assert solved([("Joe",)], [("Joe",)]).as_detail() == {
        "solved": True,
        "reason": "solved",
        "detail": "1 rows, in any order",
    }
    failed = solved([("Joe",)], [("Rose",)]).as_detail()
    assert failed["solved"] is False
    assert failed["reason"] == "value_mismatch"


def test_every_reason_a_comparison_can_give_is_a_stable_slug():
    # Counted in 2.5's taxonomy and in Phase 4's containment figures, so they may be added
    # to but must not be renamed underneath a committed result.
    reference = [("Joe",)]
    seen = {
        solved(reference, reference).reason,
        solved(reference, []).reason,
        solved([("Joe", 1)], [("Joe", 1, 2)]).reason,
        solved(reference, [("Rose",)]).reason,
        solved(reference, None, candidate_error="boom").reason,
        solved(None, reference, reference_error="boom").reason,
        solved(reference, reference, candidate_truncated=True).reason,
    }
    assert seen == {
        "solved",
        "row_count",
        "column_count",
        "value_mismatch",
        "candidate_error",
        "reference_error",
        "truncated",
    }


# --- the awkward data the substrate actually holds --------------------------------------------


def test_rows_mixing_types_in_one_column_sort_rather_than_raise():
    """SQLite is dynamically typed, so a column can hold a NULL, a number and a string.

    Sorting those directly raises `TypeError`, and unordered comparison sorts. This is the
    case that would have taken the sanity check down on real data.
    """
    reference = [(None,), (1,), ("two",), (3.5,), (b"\xff",)]
    assert solved(reference, list(reversed(reference))).solved


def test_the_replacement_character_compares_as_itself():
    """The `wta_1` decode fault, met again.

    Two of this frame's 1,034 reference queries return bytes that are not valid UTF-8;
    strict decoding would report 99.81% rather than 100.00%. The tolerance belongs to the
    connection, not to this rule: both sides are decoded with `errors="replace"`, so both
    carry U+FFFD and compare equal. **A decode fault is never a non-solve.**
    """
    assert solved([("Ma�ller",)], [("Ma�ller",)]).solved
    assert not solved([("Ma�ller",)], [("Mueller",)]).solved


def test_bytes_and_text_are_not_the_same_value():
    assert not values_equal(b"Joe", "Joe")
    assert values_equal(b"Joe", b"Joe")


# --- the sanity check, over the frame ----------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent
CHECK = json.loads((REPO / "docs" / "equivalence-check.json").read_text())
SPIDER = REPO / "data" / "spider"
FRAME = json.loads((REPO / "splits" / "frame.json").read_text())["tasks"]

needs_substrate = pytest.mark.skipif(
    not (SPIDER / "dev.json").exists(),
    reason="substrate not acquired; see docs/SUBSTRATE.md",
)


def test_the_committed_check_reports_the_whole_frame_solved_against_itself():
    """2.1's acceptance, guarded where the substrate does not exist.

    Continuous integration has no databases, so this reads the artifact
    `scripts/equivalence_check.py` wrote. The test below re-derives it wherever the
    substrate is present.
    """
    assert CHECK["frame_size"] == 1034
    assert CHECK["self_comparison"]["solved"] == 1034
    assert CHECK["self_comparison"]["rate"] == 1.0
    assert CHECK["failures"] == []


def test_the_committed_check_shows_the_permutation_control_did_something():
    """The only part of a gold-against-gold check that is not close to a tautology.

    Every unordered result that could be permuted was, and still solved; every ordered one
    that could be permuted was, and no longer did. A result with one row, or with rows that
    are all identical, cannot be permuted at all and is left out of both denominators
    rather than counted as a pass.
    """
    control = CHECK["permutation_control"]
    assert control["unordered_permuted"] > 0
    assert control["unordered_still_solved"] == control["unordered_permuted"]
    assert control["ordered_permuted"] > 0
    assert control["ordered_now_rejected"] == control["ordered_permuted"]


def test_the_committed_check_records_what_an_empty_answer_is_worth():
    # The one case where doing nothing scores, kept next to the number it is worth.
    empty = CHECK["self_comparison"]["empty_results"]
    assert empty == 49
    assert CHECK["self_comparison"]["empty_share"] == round(empty / 1034, 6)


def test_the_committed_check_used_the_tolerance_this_module_declares():
    assert CHECK["tolerance"] == {"absolute": ABSOLUTE_TOLERANCE, "relative": RELATIVE_TOLERANCE}


@needs_substrate
def test_every_gold_query_in_the_frame_solves_against_itself() -> None:
    """The acceptance criterion itself, re-derived rather than read.

    Runs where the substrate is present. What it is really testing is that the rule
    survives real data — mixed types in one column, real NULLs, aggregates that came back
    as reals, and the `wta_1` bytes that do not decode — none of which a hand-built fixture
    puts in front of it.
    """
    dev = json.loads((SPIDER / "dev.json").read_text())
    frame = set(FRAME)
    connections: dict[str, sqlite3.Connection] = {}
    unsolved: list[str] = []
    try:
        for index, item in enumerate(dev):
            task_id = f"dev-{index:04d}"
            if task_id not in frame:
                continue
            db_id = item["db_id"]
            if db_id not in connections:
                conn = sqlite3.connect(
                    f"file:{SPIDER / 'database' / db_id / f'{db_id}.sqlite'}?mode=ro", uri=True
                )
                # The decode tolerance belongs to the connection, not to the rule: both
                # sides are decoded the same way, so a decode fault is never a non-solve.
                conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
                connections[db_id] = conn
            rows = connections[db_id].execute(item["query"]).fetchall()
            verdict = compare(rows, list(rows), ordered=orders_rows(item["query"]))
            if not verdict.solved:
                unsolved.append(f"{task_id} [{db_id}] {verdict.reason}: {verdict.detail}")
    finally:
        for conn in connections.values():
            conn.close()
    assert unsolved == []
