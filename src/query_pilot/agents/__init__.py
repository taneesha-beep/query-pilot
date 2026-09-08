"""The agents this project measures against each other.

A package rather than a module, which is the flip session 4's entry named in advance: the
single-module shape `equivalence.py` established stops paying once one item needs a prompt,
a schema renderer, an extraction step and an agent that composes them — and A1's tool
layer, loop and repair are three more of the same kind.

**The dependency points one way, as everywhere else here.** This package imports
`query_pilot.client`, `query_pilot.run`, `query_pilot.sandbox` and `query_pilot.equivalence`;
none of those imports it. An agent is the one layer that knows both how a query is executed
and what makes its result the same answer — which is exactly why the sandbox and the
equivalence rule do not know about each other.
"""

from query_pilot.agents.a0 import (
    A0,
    MAX_OUTPUT_TOKENS,
    ROLE,
    SYSTEM_PROMPT,
    Attempt,
    build_prompt,
)
from query_pilot.agents.results import RESULTS_NAME, project, write_results
from query_pilot.agents.schema import (
    Column,
    ForeignKey,
    Schema,
    Table,
    read_schema,
    render_schema,
)
from query_pilot.agents.sql import NO_SQL_FOUND, Extraction, extract_sql, split_statements

__all__ = [
    "A0",
    "MAX_OUTPUT_TOKENS",
    "NO_SQL_FOUND",
    "RESULTS_NAME",
    "ROLE",
    "SYSTEM_PROMPT",
    "Attempt",
    "Column",
    "Extraction",
    "ForeignKey",
    "Schema",
    "Table",
    "build_prompt",
    "extract_sql",
    "project",
    "read_schema",
    "render_schema",
    "split_statements",
    "write_results",
]
