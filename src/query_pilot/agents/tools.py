"""The four tools A1 discovers a database with, and what bounds what they hand back.

**A1 is not given the schema. Discovering it is the work, and that is the whole difference
from A0** — which renders every table of the database into one prompt and asks once. These
four are the only way A1 learns that a table exists, what is in it, what its values look
like, and whether a query runs.

**One implementation, not two.** ``list_tables`` and ``describe_table`` read through
:mod:`query_pilot.agents.schema`, the same module and the same PRAGMAs A0's prompt is built
from, and ``describe_table`` returns the characters :func:`~query_pilot.agents.schema.render_table`
emits rather than a second rendering of the same facts. ``execute_sql`` goes through the
same :class:`~query_pilot.sandbox.Sandbox` as A0 and the reference, under the same
read-only connection, the same ``query_only`` pragma, the same 30-second deadline and the
same caps. Two agents reading different descriptions of one world would make 3.6 a
comparison of the descriptions rather than of the agents; that is constraint 41 and this
module is where it is kept.

**Pure with respect to agent state.** Every function here takes a sandbox, a database path
and a mapping of arguments, and returns a :class:`ToolResult`. Nothing holds a
conversation, a turn count, or anything about the task. The loop in :mod:`a1` owns all of
that, which is what lets 3.5 wrap these same four functions in an MCP server without
reimplementing one of them.

Two different limits meet here and they must not be confused:

- **The sandbox's caps** — 50,000 rows, 1 MiB, 30 s — bound *execution*, and they were
  derived in 2.2 against the largest legitimate result in the frame so that no cap can
  decide a verdict. They are untouched.
- **The rendering caps below** bound what a result costs *in a prompt*, which is a
  different question with a different answer: the conversation grows every turn and the
  strong endpoint serves 8,000 tokens a minute.

**A rendering cap cannot move the accuracy figure, and here is the reason rather than the
assurance:** ``execute_sql``'s result is never scored. A1's final SQL is re-executed and
compared exactly as A0's is, through the same sandbox and the same equivalence rule, so
what the model was *shown* mid-trajectory sits entirely on the prompt side of the run.

**Which failures are structured and which are not.** Everything the model can cause comes
back as a :class:`ToolResult` with ``error`` set — an unknown tool, a missing or
wrongly-typed argument, a table that does not exist, SQL that does not run, a statement
that times out. Anything that means *this project cannot read its own substrate* raises,
because that is a fault in the run rather than a move the model made, and 1.3 reserves
``executor_error`` for exactly it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from query_pilot.agents.schema import quote, read_table, read_table_names, render_table
from query_pilot.client.types import ToolCall, ToolSchema
from query_pilot.sandbox import Sandbox

__all__ = [
    "RESULT_ROWS",
    "SAMPLE_ROWS_DEFAULT",
    "SAMPLE_ROWS_MAX",
    "TOOL_NAMES",
    "TOOL_RESULT_CHARS",
    "TOOL_SCHEMAS",
    "VALUE_CHARS",
    "ToolResult",
    "call_tool",
    "describe_table",
    "execute_sql",
    "list_tables",
    "render_rows",
    "sample_rows",
]

#: How many rows of an ``execute_sql`` result the model is shown.
#:
#: **Derived, and `docs/a1-tool-sizes.json` is the derivation.** The binding condition is
#: constraint 46, which binds Phase 3 harder than it bound Phase 2: one attempt must stay
#: under the strong endpoint's 8,000 tokens a minute or the client's own bucket ends the
#: run as `pools_exhausted` with no provider having refused anything. A0's attempts were a
#: flat ~712 tokens; **A1's conversation grows every turn**, so its *last* attempt carries
#: every tool result of the trajectory and is the one that has to fit. At the measured
#: 3.265 characters a prompt token and A0's 1,024-token output ceiling held equal, one
#: attempt's whole prompt budget is **22,776 characters**, and every tool result in a
#: trajectory shares it.
#:
#: Ten rather than twenty: a 20-row result off the widest table in the working set renders
#: to **5,558 characters**, a quarter of the whole budget for one call, against **3,130**
#: at ten. What the model needs from this is whether the query ran and whether the shape is
#: plausible, and ten rows answers that. **The true row count is always reported beside the
#: rows shown**, so a capped result can never read as a complete one.
RESULT_ROWS: Final = 10

#: The default and the ceiling for ``sample_rows``, derived the same way and equal to
#: :data:`RESULT_ROWS` at the ceiling for the same reason.
#:
#: A model asking for more than the ceiling gets the ceiling and is told so, rather than an
#: error: an over-large ``limit`` is not a mistake worth spending a turn of a 150-task run
#: correcting. The default is five because five rows show how a value is *spelled*, which
#: is the whole reason this tool exists — the measured cost is a median of 633 characters
#: against a worst table's 1,809.
SAMPLE_ROWS_DEFAULT: Final = 5
SAMPLE_ROWS_MAX: Final = 10

#: The most characters one rendered value may occupy before the rest is dropped.
#:
#: Derived the way the sandbox's caps were, and the same requirement: **a cap must sit
#: above the largest legitimate value or it silently decides what the model sees.** The
#: longest TEXT value anywhere in the working set is **60 characters**
#: (`real_estate_properties.Properties`), and **0 of 111,235 TEXT values exceed 120** — so
#: this truncates nothing in this substrate and exists for the pathology the row cap cannot
#: bound, which is few rows holding very large values. That is the same pair the sandbox's
#: row cap and byte cap bound, one layer down and at a different scale.
VALUE_CHARS: Final = 120

#: A backstop on one whole tool result, after every other cap has applied.
#:
#: The row and value caps do the work; this exists so that no arrangement of columns and
#: rows can put an unbounded string into a prompt. **Above the largest legitimate result by
#: 1.92x**: the largest tool result the working set can produce is 3,130 characters — ten
#: rows of the widest table — and that is the number this has to sit above, on the same
#: reasoning as :data:`VALUE_CHARS`. If it ever binds in a real run, the row cap was the
#: wrong shape rather than this being the wrong size, and `docs/a1-tool-sizes.json` records
#: the margin so a later session can see which happened.
TOOL_RESULT_CHARS: Final = 6_000

#: What a value with no room left is replaced by. Fixed here so a test can find it.
_ELLIPSIS: Final = "..."


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What one tool call produced, and what stopped it producing more.

    ``content`` is the string the model is shown and is the *only* part of this that
    reaches a prompt. The rest is the execution record 3.4 computes trajectory metrics
    from and 4.3 reads compliance out of, and it goes to the transcript rather than to the
    model.

    Errors are **returned, not raised**, the same convention and for the same reason as
    :class:`~query_pilot.sandbox.SandboxResult`: a model writing SQL that does not run is
    an ordinary move in a trajectory that the loop must be able to answer, not an
    exception that ends a task.
    """

    content: str
    error: str | None = None
    rows_returned: int | None = None
    rows_shown: int | None = None
    truncated_by: str | None = None
    elapsed_s: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def failed(cls, message: str) -> ToolResult:
        """A structured failure whose ``content`` is what the model is told about it.

        The ``ERROR:`` prefix is deliberate and load-bearing: the model has to be able to
        tell a failed call from a call that succeeded and returned nothing, because those
        two ask for different next moves and 3.4's recovery rate counts them apart.
        """
        return cls(content=f"ERROR: {message}", error=message)


# --- rendering ------------------------------------------------------------------------


def _render_value(value: Any, *, value_chars: int = VALUE_CHARS) -> str:
    """One cell, as the model sees it.

    ``NULL`` is spelled out because an empty cell and a NULL are different facts and a
    model asked to write ``IS NULL`` needs to see which it is looking at. A BLOB is
    described rather than shown, because its bytes are not something a model can use and
    would cost the whole budget.
    """
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    # Newlines and tabs would break the one-row-per-line grid below. A pipe inside a value
    # stays as it is: the grid is a hint for the model, not a format anything parses back.
    text = " ".join(str(value).split())
    if len(text) > value_chars:
        return text[:value_chars] + _ELLIPSIS
    return text


def render_rows(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    total: int | None = None,
    max_rows: int = RESULT_ROWS,
    value_chars: int = VALUE_CHARS,
    truncated_by: str | None = None,
) -> str:
    """A result as a pipe-separated grid with a header, plus a line saying what was shown.

    ``total`` is the number of rows that actually came back, which is not the number shown.
    Saying both is the point: a model that is shown 20 rows of 4,312 and told so knows its
    query ran and returned a lot, and a model shown 20 of 20 knows it has the whole answer
    in front of it. Hiding the difference would let a capped result read as a complete one,
    which is the same failure the sandbox's ``truncated`` flag exists to prevent one layer
    down.
    """
    total = len(rows) if total is None else total
    if not rows:
        return "(no rows)"
    shown = list(rows[:max_rows])
    lines = []
    if columns:
        lines.append(" | ".join(columns))
    lines.extend(
        " | ".join(_render_value(value, value_chars=value_chars) for value in row) for row in shown
    )
    note = f"({len(shown)} of {total} rows"
    if truncated_by is not None:
        note += f"; the query was cut off by the sandbox's {truncated_by} cap"
    lines.append(note + ")")
    return "\n".join(lines)


def _bounded(content: str) -> str:
    """The backstop. Applied to every tool result on the way out, once."""
    if len(content) <= TOOL_RESULT_CHARS:
        return content
    return content[:TOOL_RESULT_CHARS] + f"\n({_ELLIPSIS} trimmed to {TOOL_RESULT_CHARS} chars)"


# --- argument checking ------------------------------------------------------------------


class _BadArgument(Exception):
    """Raised by the checks below and caught inside the tool that called them.

    Private, and it never leaves this module: every public function here returns a
    :class:`ToolResult`. A model that sends a wrong argument gets told what was wrong and
    can send the right one, which is a turn of the loop working rather than a task ending.
    """


def _required_str(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if value is None:
        raise _BadArgument(f"the {name!r} argument is required")
    if not isinstance(value, str):
        raise _BadArgument(f"the {name!r} argument must be a string, not {type(value).__name__}")
    if not value.strip():
        raise _BadArgument(f"the {name!r} argument is empty")
    return value


def _optional_int(arguments: Mapping[str, Any], name: str, default: int) -> int:
    """An integer, accepting the string a provider may have handed back instead.

    Groq returns a tool call's ``arguments`` as a JSON string and Google returns an object;
    both are normalised in `providers/`, but a model that writes ``"limit": "10"`` inside
    its own JSON produces a string on either path. Refusing that would spend a turn on a
    difference the model cannot see, so it is accepted and anything else is not.
    """
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        raise _BadArgument(f"the {name!r} argument must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise _BadArgument(f"the {name!r} argument must be an integer, not {value!r}") from None
    raise _BadArgument(f"the {name!r} argument must be an integer, not {type(value).__name__}")


def _unknown_table(table: str, known: Sequence[str]) -> ToolResult:
    """A missing table, answered with the names that do exist.

    Listing them is what makes this a move the model can recover from in one turn rather
    than a guess it has to make again. A wrong table name is the most likely mistake in a
    trajectory that has not called ``list_tables`` yet, and it is the cheapest to fix.
    """
    return ToolResult.failed(
        f"no table named {table!r}. The tables are: {', '.join(known) if known else '(none)'}"
    )


# --- the four tools ---------------------------------------------------------------------


def list_tables(sandbox: Sandbox, database: Path | str, arguments: Mapping[str, Any]) -> ToolResult:
    """Every table and how many rows it holds.

    The row count is the one fact here that is not already in the schema, and it is the one
    that changes what a model does next: an empty table is not worth describing, and a
    table with a million rows is not worth sampling blindly.
    """
    names = read_table_names(sandbox, database, str(database))
    lines = [f"{len(names)} tables:" if names else "This database has no tables."]
    for name in names:
        count = sandbox.execute(database, f"SELECT COUNT(*) FROM {quote(name)}")
        # A count that fails is reported per table rather than failing the whole call. One
        # unreadable table should not hide the nineteen readable ones from the model.
        rows = f"{count.rows[0][0]} rows" if count.ok and count.rows else "row count unavailable"
        lines.append(f"  {name} ({rows})")
    return ToolResult(content=_bounded("\n".join(lines)), rows_returned=len(names))


def describe_table(
    sandbox: Sandbox, database: Path | str, arguments: Mapping[str, Any]
) -> ToolResult:
    """One table's columns, types, primary key and foreign keys, as `CREATE TABLE`.

    The characters are :func:`~query_pilot.agents.schema.render_table`'s, which is what A0's
    prompt carries for the same table. A test asserts that describing every table of a
    database concatenates to A0's whole rendered schema, which is constraint 41 checked
    rather than asserted.
    """
    try:
        table = _required_str(arguments, "table")
    except _BadArgument as exc:
        return ToolResult.failed(str(exc))

    known = read_table_names(sandbox, database, str(database))
    if table not in known:
        return _unknown_table(table, known)
    return ToolResult(
        content=_bounded(render_table(read_table(sandbox, database, str(database), table)))
    )


def sample_rows(sandbox: Sandbox, database: Path | str, arguments: Mapping[str, Any]) -> ToolResult:
    """A few real rows from one table, so a model can see what its values look like.

    **This is the capability A0 deliberately does not have.** `render_schema` excludes row
    values on purpose, and this is where they belong — a model guessing a literal for
    ``WHERE country = ?`` is guessing between ``'USA'``, ``'United States'`` and ``'us'``
    until it has seen one.

    It is also the route Phase 4.2's planted row values take into a prompt, and A0 has no
    equivalent. 4.3's compliance figure is about A1 for that reason and is not a
    like-for-like difference between the two agents.
    """
    try:
        table = _required_str(arguments, "table")
        limit = _optional_int(arguments, "limit", SAMPLE_ROWS_DEFAULT)
    except _BadArgument as exc:
        return ToolResult.failed(str(exc))
    if limit < 1:
        return ToolResult.failed("the 'limit' argument must be at least 1")
    # Clamped rather than refused. A model asking for 500 rows wants to see the table, not
    # to be corrected, and spending a turn on the correction buys nothing.
    limit = min(limit, SAMPLE_ROWS_MAX)

    known = read_table_names(sandbox, database, str(database))
    if table not in known:
        return _unknown_table(table, known)

    result = sandbox.execute(database, f"SELECT * FROM {quote(table)} LIMIT {limit}")
    if not result.ok:
        return ToolResult.failed(f"could not sample {table!r}: {result.error}")
    content = render_rows(
        result.columns,
        result.rows,
        max_rows=limit,
        truncated_by=result.truncated_by,
    )
    return ToolResult(
        content=_bounded(f"Up to {limit} rows of {table!r}:\n{content}"),
        rows_returned=len(result.rows),
        rows_shown=min(len(result.rows), limit),
        truncated_by=result.truncated_by,
        elapsed_s=result.elapsed_s,
    )


def execute_sql(sandbox: Sandbox, database: Path | str, arguments: Mapping[str, Any]) -> ToolResult:
    """Run one statement through the 2.2 sandbox and show what came back.

    **The same sandbox A0 uses and the reference is executed in** — one implementation, so
    a query that runs here runs identically when A1's final answer is scored, and a query
    the sandbox refuses is refused for both agents for the same reason.

    An error is returned rather than raised, which is the point of the tool: a model that
    writes ``no such column: nmae``, sees that, and fixes it is the loop doing the one
    thing A0 structurally cannot. 3.4's recovery rate is counted out of exactly these.
    """
    try:
        sql = _required_str(arguments, "sql")
    except _BadArgument as exc:
        return ToolResult.failed(str(exc))

    result = sandbox.execute(database, sql)
    if not result.ok:
        return ToolResult.failed(result.error or "the statement did not run")
    content = render_rows(
        result.columns,
        result.rows,
        max_rows=RESULT_ROWS,
        truncated_by=result.truncated_by,
    )
    return ToolResult(
        content=_bounded(content),
        rows_returned=len(result.rows),
        rows_shown=min(len(result.rows), RESULT_ROWS),
        truncated_by=result.truncated_by,
        elapsed_s=result.elapsed_s,
    )


# --- the schemas the model is offered ----------------------------------------------------

#: The four tools, in the order they are offered to the model.
#:
#: The order is the order a trajectory would use them in — what exists, what is in it, what
#: it looks like, does this run — because a list is also a hint, and there is no cost to the
#: hint being the right one.
#:
#: **Every description is written for a model that has never seen this database.** A
#: parameter that is not described is a parameter that gets guessed, and 0.4's inventory
#: proved tool calling on a *one-function* schema — nothing in this repository had asked a
#: model to choose among four before 3.1.
TOOL_SCHEMAS: Final[tuple[ToolSchema, ...]] = (
    ToolSchema(
        name="list_tables",
        description=(
            "List every table in the database with the number of rows it holds. "
            "Call this first: you have not been shown the schema."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    ),
    ToolSchema(
        name="describe_table",
        description=(
            "Show one table's columns, their types, its primary key and its foreign keys, "
            "as a CREATE TABLE statement. Call it once per table you intend to use."
        ),
        parameters={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "The table name, spelled exactly as list_tables gave it.",
                }
            },
            "required": ["table"],
        },
    ),
    ToolSchema(
        name="sample_rows",
        description=(
            "Show a few real rows from one table, so you can see how its values are "
            "actually spelled before you compare against them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "The table name, spelled exactly as list_tables gave it.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"How many rows to show. Defaults to {SAMPLE_ROWS_DEFAULT}, "
                        f"and at most {SAMPLE_ROWS_MAX}."
                    ),
                },
            },
            "required": ["table"],
        },
    ),
    ToolSchema(
        name="execute_sql",
        description=(
            "Run one read-only SQLite statement and show the rows it returns, or the error "
            "it raised. Use it to check a query before you give it as your answer."
        ),
        parameters={
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "One SQLite SELECT statement, with no trailing semicolon.",
                }
            },
            "required": ["sql"],
        },
    ),
)

#: Name to implementation. The single dispatch table, and what 3.5's MCP server wraps.
_TOOLS: Final[Mapping[str, Any]] = {
    "list_tables": list_tables,
    "describe_table": describe_table,
    "sample_rows": sample_rows,
    "execute_sql": execute_sql,
}

TOOL_NAMES: Final[tuple[str, ...]] = tuple(schema.name for schema in TOOL_SCHEMAS)


def call_tool(sandbox: Sandbox, database: Path | str, call: ToolCall) -> ToolResult:
    """Dispatch one tool call. **An unknown name is answered, not raised.**

    A model that misspells a tool name has made the same kind of mistake as one that
    misspells a column, and it is recoverable in the same way — by being told what the
    names are. Raising here would end a task over a typo and would charge the loop for it.
    """
    tool = _TOOLS.get(call.name)
    if tool is None:
        return ToolResult.failed(
            f"no tool named {call.name!r}. The tools are: {', '.join(TOOL_NAMES)}"
        )
    return tool(sandbox, database, call.arguments)
