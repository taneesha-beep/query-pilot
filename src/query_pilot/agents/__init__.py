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
    ANSWER_RULES,
    MAX_OUTPUT_TOKENS,
    ROLE,
    SYSTEM_PROMPT,
    Attempt,
    build_prompt,
)
from query_pilot.agents.a1 import (
    A1,
    ANSWER,
    BUDGET,
    PROMPT_CEILING,
    PROMPT_CEILING_CHARS,
    REPAIR_LIMIT,
    TERMINATIONS,
    TOOL_CALL_LIMIT,
    TOOL_CALL_LIMIT_REACHED,
    TURN_LIMIT,
    TURN_LIMIT_REACHED,
    BudgetStopped,
    Trajectory,
    conversation_chars,
)
from query_pilot.agents.metrics import (
    METRICS_NAME,
    TaskMetrics,
    compute,
    read_task_metrics,
    write_metrics,
)
from query_pilot.agents.results import RESULTS_NAME, project, write_results
from query_pilot.agents.schema import (
    Column,
    ForeignKey,
    Schema,
    Table,
    read_schema,
    read_table,
    read_table_names,
    render_schema,
    render_table,
)
from query_pilot.agents.sql import (
    NO_SQL_FOUND,
    Extraction,
    blank_literals,
    extract_sql,
    split_statements,
)
from query_pilot.agents.tools import (
    RESULT_ROWS,
    SAMPLE_ROWS_DEFAULT,
    SAMPLE_ROWS_MAX,
    TOOL_NAMES,
    TOOL_RESULT_CHARS,
    TOOL_SCHEMAS,
    VALUE_CHARS,
    ToolResult,
    call_tool,
)
from query_pilot.agents.transcript import (
    TRANSCRIPTS_DIR,
    TranscriptWriter,
    read_trajectories,
    read_transcript,
    replay,
    transcript_path,
)
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

__all__ = [
    "A0",
    "A1",
    "ANSWER",
    "ANSWER_RULES",
    "BUDGET",
    "MAX_OUTPUT_TOKENS",
    "METRICS_NAME",
    "MULTIPLE_STATEMENTS",
    "NOT_A_QUERY",
    "NO_SQL_FOUND",
    "NO_STATEMENT",
    "PROMPT_CEILING",
    "PROMPT_CEILING_CHARS",
    "QUERY_OPENINGS",
    "REPAIR_LIMIT",
    "RESULTS_NAME",
    "RESULT_ROWS",
    "ROLE",
    "RULES",
    "SAMPLE_ROWS_DEFAULT",
    "SAMPLE_ROWS_MAX",
    "SYSTEM_PROMPT",
    "TERMINATIONS",
    "TOOL_CALL_LIMIT",
    "TOOL_CALL_LIMIT_REACHED",
    "TOOL_NAMES",
    "TOOL_RESULT_CHARS",
    "TOOL_SCHEMAS",
    "TRANSCRIPTS_DIR",
    "TURN_LIMIT",
    "TURN_LIMIT_REACHED",
    "VALUE_CHARS",
    "Attempt",
    "BudgetStopped",
    "Column",
    "Extraction",
    "ForeignKey",
    "Schema",
    "Table",
    "TaskMetrics",
    "ToolResult",
    "Trajectory",
    "TranscriptWriter",
    "Validation",
    "blank_literals",
    "build_prompt",
    "call_tool",
    "compute",
    "conversation_chars",
    "extract_sql",
    "project",
    "read_schema",
    "read_table",
    "read_table_names",
    "read_task_metrics",
    "read_trajectories",
    "read_transcript",
    "render_schema",
    "render_table",
    "repair_request",
    "replay",
    "split_statements",
    "transcript_path",
    "validate_answer",
    "write_metrics",
    "write_results",
]
