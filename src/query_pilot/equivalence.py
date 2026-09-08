"""When two result sets count as the same answer.

**This is the measurement instrument.** Every accuracy figure this project reports is this
module's opinion, so it is written before a single query is generated: deciding these rules
after seeing results is how a project talks itself into a better number. The rules and what
each one costs are set out in `docs/EQUIVALENCE.md`, with a worked example of each.

It compares **rows**, never SQL. Nothing here executes anything, opens a database, or
imports `query_pilot.client` or `query_pilot.run` — it takes two lists of tuples and
returns a :class:`Comparison`. That is what keeps the run package from ever learning what
a solve is: 2.4 drops the comparison into a task row's free-form ``detail``, which nothing
in `run/` looks inside.

The one thing here that reads SQL is :func:`orders_rows`, and it reads it only to answer a
single question — does the reference query fix the order of its own rows — because that
decides which of two comparisons applies.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "ABSOLUTE_TOLERANCE",
    "RELATIVE_TOLERANCE",
    "Comparison",
    "ResultSet",
    "compare",
    "orders_rows",
    "values_equal",
]

#: A pair of doubles is the same value when ``|a - b| <= ABSOLUTE + RELATIVE * |b|``.
#:
#: A policy choice, like a budget ceiling, and here is what it was derived from. SQLite
#: stores REAL as an IEEE-754 double, whose epsilon is 2.22e-16. The largest table in this
#: substrate is `wta_1.rankings` at 510,437 rows, so an aggregate summing a whole column
#: accumulates at worst 510437 x 2.22e-16 = 1.13e-10 of relative drift — and that is the
#: pathological sequential case, which is the only kind of disagreement two *correct*
#: formulations of the same aggregate can produce. 1e-9 sits 8.8x above that ceiling and
#: seven orders of magnitude below any difference a Spider question could mean.
#:
#: What it costs: two values differing by one part in a billion are called equal. No
#: question in this substrate has that resolution, so nothing true is lost — but the
#: tolerance is a choice and it is stated rather than buried.
ABSOLUTE_TOLERANCE: Final = 1e-9
RELATIVE_TOLERANCE: Final = 1e-9

#: What a row list is: the rows a query returned, each a tuple of column values.
ResultSet = Sequence[Sequence[Any]]

SOLVED: Final = "solved"
ROW_COUNT: Final = "row_count"
COLUMN_COUNT: Final = "column_count"
VALUE_MISMATCH: Final = "value_mismatch"
ROW_ORDER: Final = "row_order"
CANDIDATE_ERROR: Final = "candidate_error"
REFERENCE_ERROR: Final = "reference_error"
TRUNCATED: Final = "truncated"


@dataclass(frozen=True, slots=True)
class Comparison:
    """One verdict, and enough of why to be worth reading later.

    Deliberately not a bool. Phase 2.5 reads thirty failures by hand under a protocol fixed
    before reading, and Phase 4 measures containment over an attack corpus; both need to
    know *which* way a comparison failed, and a bare bool makes both of them guesswork.

    ``reason`` is one of a fixed set of slugs so failures can be counted. ``detail`` is
    prose for whoever is reading thirty of them in a row.
    """

    solved: bool
    reason: str
    detail: str = ""

    def as_detail(self) -> dict[str, Any]:
        """Shaped to drop into a ledger task row's free-form ``detail`` mapping.

        The run package writes this without looking inside it. That is the seam that keeps
        `run/` general: it records that a task produced an answer, and what the answer was
        worth is this module's business.
        """
        row: dict[str, Any] = {"solved": self.solved, "reason": self.reason}
        if self.detail:
            row["detail"] = self.detail
        return row


# --- does the reference fix its own row order? -------------------------------------------

_LITERAL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^\]]*\]")
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)


def _strip_literals(sql: str) -> str:
    """Blank out string literals, quoted identifiers and comments, keeping length.

    Length is kept so that offsets into the result still index the original, which is what
    lets paren depth be counted on the blanked text and read back against the real one.
    """
    text = _COMMENT.sub(lambda m: " " * len(m.group()), sql)
    return _LITERAL.sub(lambda m: " " * len(m.group()), text)


def orders_rows(sql: str) -> bool:
    """Does this query fix the order of the rows it returns?

    True when an ``ORDER BY`` governs the outermost ``SELECT`` — that is, one that appears
    at parenthesis depth zero. An ``ORDER BY`` inside a subquery orders that subquery and
    says nothing about the result the caller sees, so `SELECT name FROM (SELECT ... ORDER
    BY age)` is unordered and `SELECT name FROM t ORDER BY age` is not.

    **This is a lexer, not a parser**, and the limit is worth stating: it blanks string
    literals, quoted identifiers and comments, then counts parentheses. It cannot be
    confused by an `ORDER BY` inside a string or a column name, and it handles compound
    queries correctly because a `UNION`'s trailing `ORDER BY` sits at depth zero and does
    order the whole result. What it does not do is understand the query.
    """
    text = _strip_literals(sql)
    depth = 0
    guard = 0  # index of the first character not yet scanned for parentheses
    for match in _ORDER_BY.finditer(text):
        for char in text[guard : match.start()]:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
        guard = match.start()
        if depth <= 0:
            return True
    return False


# --- comparing one value to another -------------------------------------------------------


def values_equal(left: Any, right: Any) -> bool:
    """Is one cell the same as another?

    Three rules, each of which `docs/EQUIVALENCE.md` argues for and prices:

    **NULL equals NULL and equals nothing else.** SQL says the opposite, and SQL is right
    about rows and wrong about result sets: two queries that both return no value for a
    column have returned the same answer. It is emphatically not equal to 0, to the empty
    string, or to the text "NULL" — a LEFT-for-INNER join error is exactly what produces
    NULLs where the reference has none, and coercion would hide the most common wrong-join
    failure there is.

    **A number is a number.** 2 and 2.0 are the same value, because whether SQLite hands
    back an integer or a real depends on how the query was written and not on what the
    answer is: `SUM(x) / COUNT(x)` and `AVG(x)` differ in type and agree in meaning.

    **Text never coerces to a number.** '5' is not 5. This is the strict direction on
    purpose: allowing it would admit '0.50' as 0.5 and make a wrong column type invisible,
    and every strictness here makes the reported accuracy a lower bound rather than a
    flattering one.
    """
    if left is None or right is None:
        return left is None and right is None
    left_num = isinstance(left, (int, float)) and not isinstance(left, bool)
    right_num = isinstance(right, (int, float)) and not isinstance(right, bool)
    if left_num and right_num:
        if math.isnan(left) or math.isnan(right):
            # Two NaNs are the same *outcome* even though IEEE-754 says no value equals a
            # NaN. The alternative is a query that can never match its own result.
            return math.isnan(left) and math.isnan(right)
        if math.isinf(left) or math.isinf(right):
            # Tolerance cannot help here and would answer False for two identical
            # infinities, because `inf - inf` is NaN and no comparison against a NaN is
            # true. Same sign, same value.
            return left == right
        return abs(left - right) <= ABSOLUTE_TOLERANCE + RELATIVE_TOLERANCE * abs(right)
    if left_num != right_num:
        return False
    return type(left) is type(right) and left == right


def _sort_key(row: Sequence[Any]) -> tuple:
    """A total order over rows whose columns hold mixed and unorderable types.

    Real Spider results put NULLs, integers, reals, text and the occasional BLOB in the
    same column, and sorting those directly raises. Each value becomes a (rank, value)
    pair so the ranks separate the types that cannot be compared to each other, and only
    like is ever compared to like.
    """
    key: list[tuple[int, Any]] = []
    for value in row:
        if value is None:
            key.append((0, 0))
        elif isinstance(value, bool):
            # Ranked apart from the numbers on purpose, to stay consistent with
            # `values_equal`, which does not treat a bool as one. sqlite3 never returns a
            # bool, so this is a guard rather than a case.
            key.append((6, int(value)))
        elif isinstance(value, (int, float)):
            # NaN is unorderable against everything including itself, so it is ranked
            # apart rather than left to make the sort non-deterministic.
            key.append((2, float("inf")) if math.isnan(value) else (1, float(value)))
        elif isinstance(value, str):
            key.append((3, value))
        elif isinstance(value, (bytes, bytearray)):
            key.append((4, bytes(value)))
        else:
            key.append((5, repr(value)))
    return tuple(key)


def _rows_equal(left: Sequence[Any], right: Sequence[Any]) -> bool:
    return all(values_equal(a, b) for a, b in zip(left, right, strict=True))


# --- the rule -----------------------------------------------------------------------------


def compare(
    reference: ResultSet | None,
    candidate: ResultSet | None,
    *,
    ordered: bool,
    reference_error: str | None = None,
    candidate_error: str | None = None,
    reference_truncated: bool = False,
    candidate_truncated: bool = False,
) -> Comparison:
    """Is ``candidate`` the same answer as ``reference``?

    ``ordered`` comes from :func:`orders_rows` on the *reference* query, never on the
    candidate's: what the question asked for is fixed by the reference, and letting a
    candidate's own `ORDER BY` decide how it is judged would let it choose its own test.

    Errors and truncation are passed in rather than raised, because both are outcomes a
    run must count and neither is a reason to skip a task:

    - **An error is a non-solve, never a skip.** A query that does not execute did not
      answer the question. Its message is carried so 2.5 can read it.
    - **A truncated result is its own reason, not a wrong answer.** 2.2 caps rows and
      bytes; a capped candidate compared against an uncapped reference would be a false
      non-solve, and folding that into `row_count` would quietly attribute a cap's effect
      to the model. Naming it separately means 2.4 can count how many tasks the cap
      decided — and if that count is not zero, the cap gets raised rather than the accuracy
      figure silently absorbing it.
    """
    if reference_error is not None:
        # Should not arise: the frame is every task whose reference query executes. If it
        # ever does, it is a fact about the substrate and must not be filed as the
        # candidate's failure.
        return Comparison(False, REFERENCE_ERROR, f"reference query failed: {reference_error}")
    if candidate_error is not None:
        return Comparison(False, CANDIDATE_ERROR, candidate_error)
    if reference_truncated or candidate_truncated:
        which = "reference" if reference_truncated else "candidate"
        if reference_truncated and candidate_truncated:
            which = "both results"
        return Comparison(
            False, TRUNCATED, f"{which} hit a sandbox cap; the two are not comparable"
        )
    if reference is None or candidate is None:
        missing = "reference" if reference is None else "candidate"
        return Comparison(False, CANDIDATE_ERROR, f"no {missing} rows and no error given")

    if len(reference) != len(candidate):
        # Multiset, not set: a query returning five identical rows and one returning one
        # have answered different questions, and DISTINCT is a decision the reference made
        # or did not. Empty against empty falls through here as equal, and is a solve.
        return Comparison(
            False, ROW_COUNT, f"{len(candidate)} rows against the reference's {len(reference)}"
        )
    if not reference:
        return Comparison(True, SOLVED, "both results are empty")

    width = len(reference[0])
    if any(len(row) != width for row in candidate):
        widths = sorted({len(row) for row in candidate})
        return Comparison(
            False,
            COLUMN_COUNT,
            f"{widths if len(widths) > 1 else widths[0]} columns against the reference's {width}",
        )

    if ordered:
        for index, (want, got) in enumerate(zip(reference, candidate, strict=True)):
            if not _rows_equal(want, got):
                return Comparison(
                    False, VALUE_MISMATCH, f"row {index} differs: {got!r} against {want!r}"
                )
        return Comparison(True, SOLVED, f"{len(reference)} rows in the reference's own order")

    # Unordered: sort both sides on a total order and compare pairwise. For a single
    # numeric column this pairing is optimal; for wider rows it is an approximation, and
    # the only way it can be wrong is a near-tied pair of floats interleaving differently
    # on the two sides. `docs/EQUIVALENCE.md` states that, and the sanity check over the
    # frame reports whether it ever happens.
    want_sorted = sorted(reference, key=_sort_key)
    got_sorted = sorted(candidate, key=_sort_key)
    for want, got in zip(want_sorted, got_sorted, strict=True):
        if not _rows_equal(want, got):
            return Comparison(
                False, VALUE_MISMATCH, f"no match for reference row {want!r}; closest was {got!r}"
            )
    return Comparison(True, SOLVED, f"{len(reference)} rows, in any order")
