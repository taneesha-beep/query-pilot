"""Spider's difficulty label, computed from the parsed SQL in dev.json.

Spider does not ship a difficulty field. The four labels — easy, medium, hard, extra —
come from a rule the dataset's authors apply in their evaluation script, which counts
structural features of the parsed query and thresholds them. This module implements that
rule against the `sql` object already present in every dev.json item, so no SQL parsing
happens here.

The label is a **reported dimension** of this project, not merely a sampling key: accuracy
is broken out by difficulty in the README. It is also the stratification key for the
working set and the reserve set.

Two places below reproduce oddities in the reference implementation rather than correcting
them. They are marked. The point of this module is to produce *the canonical labels*, and
a label that disagrees with everyone else's published Spider numbers would be worse than
useless — so where the rule is strange, the strangeness is kept and named.

Agreement with the reference implementation was checked on all 1,034 dev items; see the
roadmap's decisions log and docs/SPLITS.md.
"""

from __future__ import annotations

from typing import Any

DIFFICULTIES = ("easy", "medium", "hard", "extra")

# Positions in the parsed representation, mirroring dev.json's own encoding.
_WHERE_OPS = ("not", "between", "=", ">", "<", ">=", "<=", "!=", "in", "like", "is", "exists")
_LIKE = _WHERE_OPS.index("like")
_NO_AGGREGATE = 0

# A condition list alternates condition units with the strings "and" / "or".
_UNITS = slice(None, None, 2)
_CONNECTORS = slice(1, None, 2)


def _carries_aggregate(unit: Any) -> bool:
    """True when a unit's leading slot is not the 'no aggregate' marker.

    Quirk, reproduced deliberately: the reference implementation calls this on several
    shapes whose leading slot is not an aggregate at all — a condition unit's leading slot
    is its NOT flag, and a raw "and"/"or" connector string has a leading character, which
    is never equal to 0 and so always counts. Both inflate the aggregate tally in ways the
    published labels already reflect.
    """
    return unit[0] != _NO_AGGREGATE


def _count_aggregates(units: Any) -> int:
    return sum(1 for unit in units if _carries_aggregate(unit))


def clause_count(sql: dict[str, Any]) -> int:
    """Component 1: WHERE, GROUP BY, ORDER BY, LIMIT, each JOIN, each OR, each LIKE."""
    count = 0
    if len(sql["where"]) > 0:
        count += 1
    if len(sql["groupBy"]) > 0:
        count += 1
    if len(sql["orderBy"]) > 0:
        count += 1
    if sql["limit"] is not None:
        count += 1
    if len(sql["from"]["table_units"]) > 0:
        count += len(sql["from"]["table_units"]) - 1  # one per join

    connectors = (
        sql["from"]["conds"][_CONNECTORS] + sql["where"][_CONNECTORS] + sql["having"][_CONNECTORS]
    )
    count += sum(1 for token in connectors if token == "or")

    conditions = sql["from"]["conds"][_UNITS] + sql["where"][_UNITS] + sql["having"][_UNITS]
    count += sum(1 for condition in conditions if condition[1] == _LIKE)
    return count


def nested_query_count(sql: dict[str, Any]) -> int:
    """Component 2: subqueries in conditions, plus INTERSECT, EXCEPT and UNION."""
    nested = []
    for condition in sql["from"]["conds"][_UNITS] + sql["where"][_UNITS] + sql["having"][_UNITS]:
        if isinstance(condition[3], dict):
            nested.append(condition[3])
        if isinstance(condition[4], dict):
            nested.append(condition[4])
    for set_operation in ("intersect", "except", "union"):
        if sql[set_operation] is not None:
            nested.append(sql[set_operation])
    return len(nested)


def shape_count(sql: dict[str, Any]) -> int:
    """The 'others' count: more than one aggregate, selected column, condition or grouping."""
    count = 0

    aggregates = _count_aggregates(sql["select"][1])
    aggregates += _count_aggregates(sql["where"][_UNITS])
    aggregates += _count_aggregates(sql["groupBy"])
    if len(sql["orderBy"]) > 0:
        ordered = [unit[1] for unit in sql["orderBy"][1] if unit[1]]
        ordered += [unit[2] for unit in sql["orderBy"][1] if unit[2]]
        aggregates += _count_aggregates(ordered)
    # Quirk, reproduced deliberately: HAVING is passed whole, connectors included.
    aggregates += _count_aggregates(sql["having"])
    if aggregates > 1:
        count += 1

    if len(sql["select"][1]) > 1:
        count += 1
    if len(sql["where"]) > 1:
        count += 1
    if len(sql["groupBy"]) > 1:
        count += 1
    return count


def difficulty_of(sql: dict[str, Any]) -> str:
    """Label one parsed query easy, medium, hard or extra."""
    clauses = clause_count(sql)
    nested = nested_query_count(sql)
    shape = shape_count(sql)

    if clauses <= 1 and shape == 0 and nested == 0:
        return "easy"
    if (shape <= 2 and clauses <= 1 and nested == 0) or (
        clauses <= 2 and shape < 2 and nested == 0
    ):
        return "medium"
    if (
        (shape > 2 and clauses <= 2 and nested == 0)
        or (2 < clauses <= 3 and shape <= 2 and nested == 0)
        or (clauses <= 1 and shape == 0 and nested <= 1)
    ):
        return "hard"
    return "extra"
