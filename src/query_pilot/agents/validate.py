"""Whether A1's final reply is the shape it was asked for, and what to say when it is not.

**This is the second of the two layers 3.3 owes, and the other one is the sandbox.** A
statement this module rejects is never executed, so a rejection is not an execution error
and never becomes one; a statement it admits still has to survive
:class:`~query_pilot.sandbox.Sandbox`, which refuses a write at the connection rather than
at the text. Phase 4.1 tests both ends of that pair. The division: *this decides whether
the model answered in the declared shape, and the sandbox decides what is allowed to run.*

**It is A1's, and A0 must never call it.** A0's 124 of 150 was taken with `extract_sql`
alone — which counts extra statements and drops them (`Extraction.dropped`) rather than
refusing them — and constraint 48 says that number stands as taken. Validating A0's output
now would silently re-score a published result. So this module is imported by :mod:`a1` and
by nothing else, and the asymmetry is deliberate rather than an oversight: **repair is one
of the things A1 has and A0 does not**, and rejecting multi-statement output without the
repair that answers it would be a penalty rather than a capability.

**Three rules, and each one is a rejection the model can act on.**

``no_statement``          the reply held no SQL at all.
``multiple_statements``   it held more than one, where exactly one was asked for.
``not_a_query``           the one it held does not open a read-only query.

The last is a **whitelist and not a blocklist**, which is the only defensible direction: a
list of forbidden openings is a list with a hole in it, and the hole is found by the thing
you were guarding against.

**What it actually catches is narrower than it first looks, and worth knowing exactly.**
`sql.extract_sql` only ever begins a statement at ``SELECT`` or at a real ``WITH ... AS (``,
so a reply holding nothing but ``DROP TABLE t`` or ``PRAGMA table_info(t)`` never reaches
this rule at all — it is a ``no_statement`` one rule earlier. What *does* reach here is a
statement that **contains** a query without **opening** with one, which is precisely the
dangerous shape: ``INSERT INTO other SELECT * FROM secrets``,
``CREATE TABLE t AS SELECT ...``, ``CREATE VIEW v AS SELECT ...``,
``DELETE FROM t WHERE id IN (SELECT ...)``. Those are Phase 4.2's exfiltrate-and-destroy
placements written as one statement, and this is the layer that refuses them before the
sandbox is asked to.

The whitelist is therefore ``SELECT`` and ``WITH`` — optionally behind parentheses, because
``(SELECT ...) UNION (SELECT ...)`` is a real answer. ``VALUES`` is deliberately **not**
here: SQLite accepts a bare ``VALUES (1), (2)``, but the extractor cannot produce one, and
widening `sql._OPENING` to admit it would change the module A0's 124 of 150 was taken with.
Constraint 48 makes that not worth a statement no reference in this substrate uses.

**Why a rejection reports ``no_sql`` rather than a slug of its own.** The equivalence slug
set may be extended but never renamed underneath a committed result, and
`equivalence.ROW_ORDER` records the decision that a ninth slug is not to be started now that
`results/a0-working.json` exists. A rejected reply produced no statement this project would
run, which is what ``no_sql`` says. **Which rule rejected it is carried separately**, as
``validation_rule`` in the task row's free-form ``detail``, so 3.6 can count the three apart
without either parsing prose or moving a slug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from query_pilot.agents.sql import NO_SQL_FOUND, blank_literals, extract_sql

__all__ = [
    "MULTIPLE_STATEMENTS",
    "NOT_A_QUERY",
    "NO_STATEMENT",
    "QUERY_OPENINGS",
    "RULES",
    "Validation",
    "repair_request",
    "validate_answer",
]

#: The reply held no SQL statement at all.
NO_STATEMENT: Final = "no_statement"
#: It held more than one, and the answer rules ask for exactly one.
MULTIPLE_STATEMENTS: Final = "multiple_statements"
#: The statement it held does not open a read-only query.
NOT_A_QUERY: Final = "not_a_query"

RULES: Final = (NO_STATEMENT, MULTIPLE_STATEMENTS, NOT_A_QUERY)

#: The only keywords a statement may open with. **A whitelist. See the module docstring**,
#: which says what this can and cannot reach and why ``VALUES`` is not among them.
QUERY_OPENINGS: Final = ("SELECT", "WITH")

#: The first bare word of a statement, once literals and comments are gone and any opening
#: parentheses of a compound query have been stepped over.
_FIRST_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


@dataclass(frozen=True, slots=True)
class Validation:
    """A verdict on one reply, and — when it failed — enough to ask again with.

    ``reason`` is written for the **record**: it is what reaches the task row's ``detail``
    and what a person reading `docs/FAILURES.md` sees. :func:`repair_request` is written for
    the **model**. Keeping them apart is deliberate; one sentence trying to be both ends up
    being neither, and the repair message is a prompt that a later session may want to
    change without changing what a committed result said.
    """

    sql: str | None
    rule: str | None = None
    reason: str | None = None
    statements: int = 0
    fenced: bool = False

    @property
    def ok(self) -> bool:
        return self.rule is None

    @property
    def dropped(self) -> int:
        """Statements beyond the first. **Recorded, and no longer silently discarded.**

        `extract_sql` counts these and keeps the first, which is what A0 does and what A0's
        result was taken under. For A1 the same count is a rejection, so this exists to keep
        `Trajectory.detail`'s ``dropped_statements`` meaning the same thing for both agents.
        """
        return max(self.statements - 1, 0)


def _opening_word(sql: str) -> str:
    """The keyword a statement opens with, or ``""`` if it opens with nothing word-shaped.

    Literals and comments are blanked first, so a reply whose statement begins with a
    comment is read by what follows the comment rather than refused for it. Leading
    parentheses are stepped over so that a parenthesised compound query is not mistaken for
    a statement with no keyword at all.
    """
    blanked = blank_literals(sql).lstrip().lstrip("(").lstrip()
    match = _FIRST_WORD.match(blanked)
    return match.group(0).upper() if match else ""


def validate_answer(text: str) -> Validation:
    """Is this reply a single read-only SQL statement, and if not, what is wrong with it.

    Runs the three rules in the order a reply fails them: there has to be a statement before
    there can be one too many, and there has to be exactly one before its opening keyword
    means anything.
    """
    extraction = extract_sql(text)
    if extraction.sql is None:
        # NO_SQL_FOUND word for word, so that this rule and A0's own no-SQL failures read
        # identically in `detail` -- they are the same failure and 2.5's protocol counted
        # them under that sentence.
        return Validation(
            None,
            rule=NO_STATEMENT,
            reason=extraction.reason or NO_SQL_FOUND,
            fenced=extraction.fenced,
        )

    statements = extraction.dropped + 1
    if extraction.dropped:
        return Validation(
            None,
            rule=MULTIPLE_STATEMENTS,
            reason=(f"the response held {statements} SQL statements and exactly one was required"),
            statements=statements,
            fenced=extraction.fenced,
        )

    opening = _opening_word(extraction.sql)
    if opening not in QUERY_OPENINGS:
        return Validation(
            None,
            rule=NOT_A_QUERY,
            reason=(
                f"the statement opens with {opening or '(no keyword)'}, and an answer must "
                f"open with one of: {', '.join(QUERY_OPENINGS)}"
            ),
            statements=statements,
            fenced=extraction.fenced,
        )

    return Validation(extraction.sql, statements=statements, fenced=extraction.fenced)


def repair_request(validation: Validation) -> str:
    """What the model is told, to give it one chance to answer in the declared shape.

    **The validation error is fed back verbatim inside it**, which is 3.3's requirement and
    not decoration: a model told only "that was wrong" repeats itself, and a model told
    which rule it broke has something to act on. The instruction that follows is deliberately
    the same demand `a0.ANSWER_RULES` already made rather than a new one — this is a second
    chance at the original question, not a different question.
    """
    if validation.ok:  # pragma: no cover - a caller asking to repair a valid answer is a bug
        raise ValueError("nothing to repair: the answer passed validation")
    return (
        f"Your reply was rejected: {validation.reason}.\n"
        "\n"
        "Reply again with your answer to the original question as a single SQLite SELECT "
        "statement and nothing else. No explanation, no code fence, no semicolon, and one "
        "statement only."
    )
