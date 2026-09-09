"""The same four tools A1 uses, over MCP on stdio. **Wrapped, never reimplemented.**

    uv run --extra mcp python -m query_pilot.mcp_server --database-root data/spider/database \\
        --db-id concert_singer

**One implementation of the tools, and this module holds none of it.** Every function here
is four lines that call :mod:`query_pilot.agents.tools` and hand back the string it returns.
That is the whole point of 3.5: 3.1 wrote the tools pure with respect to agent state —
sandbox in, database path in, :class:`~query_pilot.agents.tools.ToolResult` out, no
conversation and no turn count anywhere — precisely so that a second caller could be added
without a second copy. A tool reimplemented here would make the MCP surface a *different*
four tools that happen to share names, and the difference would only ever be found by
someone debugging a wrong answer.

**What it turns the tools into.** Closures over this project's state become a described,
versioned interface that anything speaking MCP can list and call: the schemas leave the
process, the descriptions are the same descriptions the model is offered, and the project
becomes demoable from something other than its own code.

**The database it serves is a copy, and every 2.2 control still applies.** ``--db-id`` goes
through :class:`~query_pilot.sandbox.SubstrateCopies`, so the substrate itself is never
opened; ``execute_sql`` goes through the same :class:`~query_pilot.sandbox.Sandbox` as A0,
A1 and the reference, under the same read-only connection, the same ``query_only`` pragma,
the same 30-second deadline and the same row and byte caps. An external client pointed at
this server has exactly the reach A1 has and no more, which is what makes it a sandbox
database rather than a database.

**One server, one database.** The tools' schemas carry no ``db_id`` argument, and adding one
here would make the MCP surface advertise a fifth parameter the model is never offered
locally — so the database is fixed when the process starts. It is also the honest shape for
a demo: the thing being shown is what a model can do with one database it has not been shown.

**What crosses the wire is the string, and that is a stated loss.** Each tool returns
``ToolResult.content`` — the characters A1 would put in a prompt — so an external client
renders the same grid the model reads. The rest of a ``ToolResult`` is the *execution
record*: ``rows_returned``, ``rows_shown``, ``truncated_by``, ``elapsed_s``. Those do not
survive the trip, because a structured return would replace the readable text with JSON.
**3.4's recovery rate reads exactly those fields out of the transcript**, so a trajectory
driven over MCP cannot produce a complete one — which is a second and much harder reason,
beyond process cost, that 3.6 runs A1 against the local tools. See
:mod:`query_pilot.agents.mcp_tools`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import Field

from query_pilot.agents import tools
from query_pilot.agents.tools import SAMPLE_ROWS_DEFAULT, TOOL_SCHEMAS
from query_pilot.sandbox import CopyScope, Sandbox, SubstrateCopies

__all__ = ["SERVER_NAME", "SQL_ARGUMENT", "TABLE_ARGUMENT", "build_server", "main"]

#: What an external client sees this server called. Fixed here so `docs/MCP.md` and a test
#: can both name it without either becoming the source of the other.
SERVER_NAME: Final = "query-pilot"

_DESCRIPTIONS: Final = {schema.name: schema.description for schema in TOOL_SCHEMAS}


#: The one wording, taken from the schemas the model is offered locally.
#:
#: The parameter descriptions below are written as literals rather than read from here,
#: because `from __future__ import annotations` makes an annotation a string and a runtime
#: expression inside ``Annotated`` would have to survive pydantic resolving it lazily. So the
#: equality is held by a test instead -- the same technique that pins A0's system prompt.
TABLE_ARGUMENT: Final = "The table name, spelled exactly as list_tables gave it."
SQL_ARGUMENT: Final = "One SQLite SELECT statement, with no trailing semicolon."


def build_server(database: Path, *, sandbox: Sandbox | None = None) -> Any:
    """The four tools, bound to one database. ``mcp`` is imported here, not at module scope.

    Deferred because `mcp` is an **optional extra**: it pulls 27 packages against this
    project's one runtime dependency, and nothing in the measured path needs it. Importing it
    at module scope would make a test collection that merely touches this module fail on an
    install that did not ask for the extra.
    """
    from mcp.server.mcpserver import MCPServer

    box = sandbox or Sandbox()
    server = MCPServer(name=SERVER_NAME, instructions=_INSTRUCTIONS.format(database=database.stem))

    def list_tables() -> str:
        return tools.list_tables(box, database, {}).content

    def describe_table(
        table: Annotated[
            str, Field(description="The table name, spelled exactly as list_tables gave it.")
        ],
    ) -> str:
        return tools.describe_table(box, database, {"table": table}).content

    def sample_rows(
        table: Annotated[
            str, Field(description="The table name, spelled exactly as list_tables gave it.")
        ],
        limit: Annotated[int, Field(description="How many rows to show.")] = SAMPLE_ROWS_DEFAULT,
    ) -> str:
        return tools.sample_rows(box, database, {"table": table, "limit": limit}).content

    def execute_sql(
        sql: Annotated[
            str, Field(description="One SQLite SELECT statement, with no trailing semicolon.")
        ],
    ) -> str:
        return tools.execute_sql(box, database, {"sql": sql}).content

    for function in (list_tables, describe_table, sample_rows, execute_sql):
        server.add_tool(function, description=_DESCRIPTIONS[function.__name__])
    return server


_INSTRUCTIONS: Final = (
    "Four read-only tools over one SQLite database, {database}. You have not been shown its "
    "schema: call list_tables first, describe_table for the tables you intend to use, "
    "sample_rows when you need to see how a value is spelled, and execute_sql to run a "
    "query. Every statement runs against a copy, read-only, with a 30-second deadline and "
    "row and byte caps."
)


def main(argv: list[str] | None = None) -> int:
    import asyncio

    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--db-id",
        help="a database under --database-root, served as a COPY through SubstrateCopies",
    )
    source.add_argument(
        "--database",
        type=Path,
        help="a .sqlite file to serve directly. Read-only either way, but not copied.",
    )
    parser.add_argument(
        "--database-root",
        type=Path,
        default=Path("data/spider/database"),
        help="where --db-id is looked up (default: data/spider/database)",
    )
    options = parser.parse_args(argv)

    # The copies object outlives this call deliberately: it owns the temporary directory the
    # served copy lives in, and letting it be collected would delete the database mid-session.
    copies: SubstrateCopies | None = None
    if options.db_id:
        if not (options.database_root / options.db_id).exists():
            print(
                f"no database {options.db_id!r} under {options.database_root}; "
                "see docs/SUBSTRATE.md",
                file=sys.stderr,
            )
            return 2
        copies = SubstrateCopies(options.database_root, scope=CopyScope.RUN)
        # Under RUN scope the copy is made once and outlives the `with`, which is what a
        # long-lived server needs: a TASK-scoped copy would be deleted on the way out and
        # every tool call would then open a path that no longer exists.
        with copies.for_task(options.db_id, task_id="mcp") as path:
            database = path
    else:
        database = options.database
        if not database.exists():
            print(f"no such database: {database}", file=sys.stderr)
            return 2

    try:
        asyncio.run(build_server(database).run_stdio_async())
    finally:
        if copies is not None:
            copies.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
