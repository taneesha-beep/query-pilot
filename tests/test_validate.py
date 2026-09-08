"""3.3's validator: which replies are the declared shape, and what the model is told.

**The rules are cheap and the consequences are not.** A rule that admits something it should
not sends a statement to the sandbox that will fail there instead, which is a worse failure
because it reads as the model's SQL mistake rather than as its shape mistake. A rule that
rejects something it should not turns a correct answer into a `no_sql`, spends a repair turn
on nothing, and moves A1's accuracy figure down for a reason that is this project's.

So every rule is tested from **both** sides: what it admits and what it rejects. That is the
same requirement the roadmap puts on a chosen limit, and a validator is a chosen limit made
of words.
"""

from __future__ import annotations

import pytest

from query_pilot.agents.a0 import ANSWER_RULES
from query_pilot.agents.sql import NO_SQL_FOUND, extract_sql
from query_pilot.agents.validate import (
    MULTIPLE_STATEMENTS,
    NO_STATEMENT,
    NOT_A_QUERY,
    QUERY_OPENINGS,
    RULES,
    Validation,
    repair_request,
    validate_answer,
)

# --- what a valid answer looks like -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "SELECT name FROM singer",
        "select name from singer",
        "  SELECT name FROM singer  ",
        "```sql\nSELECT name FROM singer\n```",
        "```\nSELECT name FROM singer\n```",
        "Here is the query: SELECT name FROM singer",
        "WITH old AS (SELECT * FROM singer WHERE age > 40) SELECT name FROM old",
        "(SELECT a FROM t) UNION (SELECT b FROM u)",
        "SELECT name FROM singer ORDER BY age DESC",
        # A trailing semicolon closes the only statement rather than opening a second one.
        "SELECT name FROM singer;",
        # A semicolon inside a literal is a value, not a separator -- `split_statements`
        # already knows this and the validator must not have re-learned it wrongly.
        "SELECT 'a;b' FROM singer",
    ],
)
def test_a_single_query_is_admitted(text: str) -> None:
    """Every shape a model actually produces, and the sql module already parses."""
    validation = validate_answer(text)
    assert validation.ok, validation.reason
    assert validation.rule is None
    assert validation.sql is not None
    assert validation.dropped == 0


def test_an_admitted_answer_is_the_statement_extract_sql_found() -> None:
    """Validation decides *whether*, never *what*. A validator that re-parsed would be a
    second extractor quietly disagreeing with the one A0's result was taken under.
    """
    text = "Here is the query:\n```sql\nSELECT name FROM singer\n```\nHope that helps."
    assert validate_answer(text).sql == extract_sql(text).sql


# --- no_statement -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["", "   ", "I cannot answer that question.", "```\n\n```", "-- nothing here"],
)
def test_a_reply_with_no_statement_is_rejected(text: str) -> None:
    validation = validate_answer(text)
    assert validation.rule == NO_STATEMENT
    assert validation.sql is None


def test_no_statement_reports_the_same_sentence_a0_reports() -> None:
    """`detail` has to read identically for the same failure across both agents.

    2.5 read twenty-six non-solves by hand under a protocol that keyed on this sentence, and
    4.4 will extend that catalog. A second wording for one failure would split one row of it
    into two.
    """
    assert validate_answer("I cannot answer that.").reason == NO_SQL_FOUND


# --- multiple_statements ------------------------------------------------------------------


def test_two_statements_are_rejected_where_extract_sql_would_drop_one() -> None:
    """**This is the behaviour 3.3 changes, and it is A1's alone.**

    `extract_sql` counts extra statements and keeps the first, which is what A0 does and what
    124 of 150 was taken under. For A1 the same input is a rejection that earns a repair.
    """
    text = "SELECT name FROM singer; SELECT age FROM singer"
    assert extract_sql(text).sql == "SELECT name FROM singer"
    assert extract_sql(text).dropped == 1

    validation = validate_answer(text)
    assert validation.rule == MULTIPLE_STATEMENTS
    assert validation.sql is None
    assert validation.statements == 2
    assert validation.dropped == 1


def test_the_rejection_says_how_many_statements_there_were() -> None:
    """A model told "too many" writes the same thing again; one told "three" does not."""
    reason = validate_answer("SELECT 1; SELECT 2; SELECT 3").reason or ""
    assert "3 SQL statements" in reason


# --- not_a_query --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Every one of these **contains** a query without **opening** with one, which is the
        # only shape that reaches this rule -- see the two tests below.
        "```sql\nINSERT INTO other SELECT * FROM singer\n```",
        "```sql\nCREATE TABLE copy AS SELECT * FROM singer\n```",
        "```sql\nCREATE VIEW v AS SELECT name FROM singer\n```",
        "```sql\nDELETE FROM singer WHERE id IN (SELECT id FROM singer)\n```",
        "```sql\nUPDATE singer SET age = (SELECT MAX(age) FROM singer)\n```",
        "```sql\nREPLACE INTO singer SELECT * FROM singer\n```",
    ],
)
def test_a_statement_that_is_not_a_query_is_rejected(text: str) -> None:
    """The answer layer of the two 3.3 owes. The sandbox is the other, and refuses these at
    the connection with `query_only` rather than at the text — Phase 4.1 tests both ends.

    **These are Phase 4.2's exfiltrate-and-destroy placements written as one statement**, and
    they are exactly what this rule exists for: a model that has been told to copy a table
    somewhere writes `INSERT INTO other SELECT ...`, which contains a perfectly good query.
    """
    validation = validate_answer(text)
    assert validation.rule == NOT_A_QUERY
    assert validation.sql is None


@pytest.mark.parametrize(
    "text",
    [
        "```sql\nDROP TABLE singer\n```",
        "```sql\nPRAGMA table_info(singer)\n```",
        "```sql\nATTACH DATABASE 'other.db' AS other\n```",
        "```sql\nALTER TABLE singer ADD COLUMN x INT\n```",
    ],
)
def test_a_statement_with_no_query_in_it_at_all_never_reaches_the_third_rule(text: str) -> None:
    """It is refused, but one rule earlier, and a reader has to know which.

    `extract_sql` only ever begins a statement at a SELECT or a real `WITH ... AS (`, so a
    reply holding nothing but a DROP has no statement *this project would run* — which is
    what `no_statement` says. Asserting the wrong rule here would have hidden that the third
    rule is narrower than it reads, and 3.6 counts the three apart.
    """
    validation = validate_answer(text)
    assert validation.rule == NO_STATEMENT
    assert validation.sql is None


def test_the_rejection_names_the_keyword_and_the_ones_that_are_allowed() -> None:
    reason = validate_answer("```sql\nINSERT INTO other SELECT * FROM singer\n```").reason or ""
    assert "INSERT" in reason
    for opening in QUERY_OPENINGS:
        assert opening in reason


def test_a_keyword_inside_a_literal_does_not_decide_the_opening() -> None:
    """The rule reads blanked text, so a value spelled like a keyword is a value."""
    assert validate_answer("SELECT 'DROP TABLE singer' AS x").ok


def test_a_statement_opening_with_a_comment_is_read_by_what_follows_it() -> None:
    """Refusing a query for its own comment would be this rule inventing a rule."""
    assert validate_answer("```sql\n-- the answer\nSELECT name FROM singer\n```").ok


def test_a_parenthesised_compound_query_is_not_read_as_having_no_keyword() -> None:
    assert validate_answer("((SELECT a FROM t) UNION (SELECT b FROM u))").ok


# --- the order the rules run in -----------------------------------------------------------


def test_a_reply_that_breaks_two_rules_reports_the_earlier_one() -> None:
    """There has to be exactly one statement before its opening keyword means anything, so
    a two-statement reply whose first statement is not a query is reported as the count.

    Deliberate rather than incidental: the repair message names one thing to fix, and the
    count is the one the model can act on without guessing which statement was meant.
    """
    validation = validate_answer(
        "```sql\nINSERT INTO o SELECT * FROM singer; SELECT name FROM singer\n```"
    )
    assert validation.rule == MULTIPLE_STATEMENTS


def test_every_rule_slug_is_reachable() -> None:
    """A rule nothing can produce is a rule that is not enforced. 3.6 counts these apart."""
    produced = {
        validate_answer("no sql here").rule,
        validate_answer("SELECT 1; SELECT 2").rule,
        validate_answer("```sql\nINSERT INTO o SELECT * FROM t\n```").rule,
    }
    assert produced == set(RULES)


# --- the repair message -------------------------------------------------------------------


def test_the_repair_request_carries_the_validation_error_verbatim() -> None:
    """3.3's requirement, and not decoration: a model told only "that was wrong" repeats
    itself, and one told which rule it broke has something to act on.
    """
    validation = validate_answer("SELECT 1; SELECT 2")
    assert (validation.reason or "") in repair_request(validation)


def test_the_repair_request_repeats_the_original_demand_rather_than_a_new_one() -> None:
    """A second chance at the same question, not a different question. If this drifts from
    `ANSWER_RULES` the two agents stop being comparable on the thing they were asked for.
    """
    message = repair_request(validate_answer("nothing here"))
    assert "single SQLite SELECT statement" in message
    assert "one statement only" in message
    assert "Do not explain it" in ANSWER_RULES


def test_repairing_a_valid_answer_is_refused() -> None:
    """A caller asking for this has a bug, and a message asking a model to fix a correct
    answer would spend a turn making it worse.
    """
    with pytest.raises(ValueError):
        repair_request(Validation("SELECT 1"))
