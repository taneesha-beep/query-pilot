"""3.5: the four tools over MCP, and A1 driving a whole task through them.

**Every test here spawns the real server as a subprocess and speaks real MCP over stdio.**
Nothing is faked, and nothing makes a network call — the server is a local process reading a
local sqlite copy, so constraint 5 holds: no key, no network, no quota.

What these are for, in the order the acceptance asks for it: that the server lists **exactly**
the four tools with **exactly** the descriptions the model is offered locally; that what comes
back over the wire is the **same characters** the in-process tool produces, which is what
"wrapped, not reimplemented" means when it is checked rather than asserted; that the 2.2
controls still hold through the wire; and that A1 can be a client of it end to end.

The external-client half of the acceptance cannot be a test. `docs/MCP.md` carries the
configuration and `docs/mcp-claude-desktop.png` is the evidence.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the `mcp` extra is not installed")

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from query_pilot.agents import tools
from query_pilot.agents.a1 import A1
from query_pilot.agents.mcp_tools import ERROR_PREFIX, McpTools, server_command
from query_pilot.agents.toolcaller import LocalTools, ToolCaller
from query_pilot.agents.tools import TOOL_NAMES, TOOL_SCHEMAS
from query_pilot.client.types import ToolCall
from query_pilot.mcp_server import SERVER_NAME, SQL_ARGUMENT, TABLE_ARGUMENT
from query_pilot.run import COMPLETE, Run, RunConfig, RunLedger, read_rows
from query_pilot.sandbox import Sandbox, database_path
from query_pilot.tasks import Task

QUESTION = "What are the names of every singer, oldest first?"
REFERENCE = "SELECT name FROM singer ORDER BY age DESC"


def _make(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE singer (id INT PRIMARY KEY, name TEXT NOT NULL, age INT);
            INSERT INTO singer VALUES (1, 'Joe', 30), (2, 'Rose', 41);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return _make(tmp_path / "singer.sqlite")


@pytest.fixture
def database_root(tmp_path: Path) -> Path:
    root = tmp_path / "database"
    _make(database_path(root, "concert"))
    return root


class _Session:
    """An initialised client session against a server for one database."""

    def __init__(self, database: Path) -> None:
        self.database = database

    async def __aenter__(self) -> ClientSession:
        argv = server_command(self.database)
        self._client = stdio_client(StdioServerParameters(command=argv[0], args=argv[1:]))
        reader, writer = await self._client.__aenter__()
        self._session = ClientSession(reader, writer)
        session = await self._session.__aenter__()
        self.initialized = await session.initialize()
        return session

    async def __aexit__(self, *exc: object) -> None:
        await self._session.__aexit__(*exc)
        await self._client.__aexit__(*exc)


async def _text(session: ClientSession, name: str, arguments: dict) -> str:
    result = await session.call_tool(name, arguments)
    return "\n".join(b.text for b in result.content if getattr(b, "text", None) is not None)


# --- the interface the server describes ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_server_lists_exactly_the_four_tools(database) -> None:
    """**The acceptance's first half.** Four, in the order they are offered locally — the
    order is a hint to the model and there is no cost to the hint being the right one.
    """
    async with _Session(database) as session:
        listed = await session.list_tools()
    assert tuple(tool.name for tool in listed.tools) == TOOL_NAMES
    assert len(TOOL_NAMES) == 4


@pytest.mark.asyncio
async def test_every_description_is_the_one_the_model_is_offered_locally(database) -> None:
    """A description written twice is a description that drifts, and the two agents' tools
    would then differ by a sentence nobody meant to change.
    """
    async with _Session(database) as session:
        listed = await session.list_tools()
    described = {tool.name: tool.description for tool in listed.tools}
    assert described == {schema.name: schema.description for schema in TOOL_SCHEMAS}


@pytest.mark.asyncio
async def test_the_parameters_are_the_same_parameters(database) -> None:
    """Names, requiredness and types. The MCP schema is derived from the Python signature
    rather than handed over verbatim, so this is where the derivation is checked against the
    schema the model actually sees.
    """
    async with _Session(database) as session:
        listed = await session.list_tools()
    served = {tool.name: tool.input_schema for tool in listed.tools}
    for schema in TOOL_SCHEMAS:
        properties = schema.parameters.get("properties") or {}
        assert set(served[schema.name]["properties"]) == set(properties)
        assert set(served[schema.name].get("required") or []) == set(schema.parameters["required"])
        for name, spec in properties.items():
            assert served[schema.name]["properties"][name]["type"] == spec["type"]


def test_the_pinned_parameter_descriptions_are_the_schemas_own() -> None:
    """`mcp_server` writes these as literals because `from __future__ import annotations`
    turns an annotation into a string, and a runtime expression inside `Annotated` would have
    to survive pydantic resolving it lazily. So the equality is a test — the same technique
    that pins A0's system prompt, and for the same reason.
    """
    properties = {schema.name: schema.parameters["properties"] for schema in TOOL_SCHEMAS}
    assert properties["describe_table"]["table"]["description"] == TABLE_ARGUMENT
    assert properties["sample_rows"]["table"]["description"] == TABLE_ARGUMENT
    assert properties["execute_sql"]["sql"]["description"] == SQL_ARGUMENT


@pytest.mark.asyncio
async def test_the_server_names_itself_what_the_documentation_says(database) -> None:
    """`docs/MCP.md` tells the author to look for this name in an external client's list of
    servers, so the name a client is actually told has to be the one the document names.
    """
    opened = _Session(database)
    async with opened:
        pass
    assert opened.initialized.server_info.name == SERVER_NAME == "query-pilot"


# --- wrapped, not reimplemented -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("list_tables", {}),
        ("describe_table", {"table": "singer"}),
        ("sample_rows", {"table": "singer", "limit": 2}),
        ("execute_sql", {"sql": "SELECT name FROM singer ORDER BY age DESC"}),
    ],
)
async def test_what_crosses_the_wire_is_the_same_characters_the_local_tool_produces(
    database, name, arguments
) -> None:
    """**"Wrapped, not reimplemented", checked rather than asserted.**

    A second implementation that happened to agree on these four calls would still be a
    second implementation — but one that disagrees on any of them is caught here, and the
    only way to keep this passing is to keep calling the same function.
    """
    local = tools.call_tool(Sandbox(), database, ToolCall(id="c", name=name, arguments=arguments))
    async with _Session(database) as session:
        assert await _text(session, name, arguments) == local.content


@pytest.mark.asyncio
async def test_a_tool_error_crosses_as_an_error_the_caller_can_act_on(database) -> None:
    """Errors are returned, not raised — the convention that makes recovery measurable — and
    the `ERROR:` prefix is what survives the trip to say so.
    """
    async with _Session(database) as session:
        text = await _text(session, "execute_sql", {"sql": "SELECT nmae FROM singer"})
    assert text.startswith(ERROR_PREFIX)
    assert "nmae" in text


# --- the 2.2 controls, through the wire ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM singer",
        "UPDATE singer SET age = 1",
        "INSERT INTO singer VALUES (3, 'x', 1)",
        "DROP TABLE singer",
        "CREATE TABLE t (x INT)",
    ],
)
async def test_a_write_is_refused_through_the_server(database, sql) -> None:
    """**An external client pointed at this has exactly A1's reach and no more**, which is
    what makes it a sandbox database rather than a database. Same connection, same
    `query_only` pragma, same everything: the server adds a transport and no permissions.
    """
    async with _Session(database) as session:
        assert (await _text(session, "execute_sql", {"sql": sql})).startswith(ERROR_PREFIX)
    # And the database is unchanged, which is the claim a refusal is only evidence for.
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("SELECT COUNT(*) FROM singer").fetchone()[0] == 2
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_an_unknown_table_is_answered_with_the_names_that_exist(database) -> None:
    """A recoverable move, and it stays recoverable over the wire."""
    async with _Session(database) as session:
        text = await _text(session, "describe_table", {"table": "stadium"})
    assert text.startswith(ERROR_PREFIX)
    assert "singer" in text


# --- A1 as a client of the server ---------------------------------------------------------


async def _run_over(caller: ToolCaller, tmp_path: Path, database_root: Path, client) -> dict:
    task = Task("dev-0003", "concert", QUESTION, REFERENCE)
    ledger = RunLedger("20260908-000000-mcpmcp", root=tmp_path / "runs")
    config = RunConfig.start(
        "A1",
        [task.task_id],
        token_ceiling=1_000_000,
        wall_clock_ceiling_s=3_600,
        run_id="20260908-000000-mcpmcp",
    )
    agent = A1(client, [task], database_root, ledger.directory, tools=caller)
    try:
        with ledger:
            await Run(config, ledger).execute(agent)
    finally:
        # `aclose`, not `close`: a ToolCaller may hold subprocesses and can only be released
        # from inside the event loop.
        await agent.aclose()
    return next(r for r in read_rows(ledger.path) if r["kind"] == "task")


class _Stub:
    """Scripted responses. The provider is stubbed; **the tools are not.**"""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.handed: list[list] = []

    async def complete(self, role, messages, tools=None, *, max_output_tokens=512, **kwargs):
        from query_pilot.client.types import Completion

        self.handed.append(list(messages))
        text, calls = self.responses.pop(0)
        return Completion(
            text=text,
            tool_calls=tuple(calls),
            prompt_tokens=400,
            completion_tokens=20,
            provider="groq",
            model="openai/gpt-oss-120b",
            pool="GROQ_API_KEY",
            latency_s=0.6,
            finish_reason="tool_calls" if calls else "stop",
        )


def _script():
    return (
        ("", [ToolCall(id="c1", name="list_tables", arguments={})]),
        ("", [ToolCall(id="c2", name="describe_table", arguments={"table": "singer"})]),
        (REFERENCE, []),
    )


@pytest.mark.asyncio
async def test_a1_solves_a_whole_task_over_mcp(tmp_path, database_root) -> None:
    """**The agent as one client of the server, end to end and for real.**

    Every tool call in this trajectory is a JSON-RPC round trip to a subprocess. The task
    solves, which is the point: the seam is a capability rather than a sentence in a roadmap.
    """
    row = await _run_over(McpTools(), tmp_path, database_root, _Stub(*_script()))
    assert row["status"] == COMPLETE
    assert row["detail"]["solved"] is True
    assert row["detail"]["tool_calls"] == 2
    assert row["detail"]["tool_calls_by_name"] == {"list_tables": 1, "describe_table": 1}


@pytest.mark.asyncio
async def test_the_two_callers_produce_the_same_trajectory(tmp_path, database_root) -> None:
    """The seam must not be a difference. Same script, same verdict, same counts — the only
    thing that changed is where the four functions ran.
    """
    over_mcp = await _run_over(McpTools(), tmp_path, database_root, _Stub(*_script()))
    over_local = await _run_over(LocalTools(), tmp_path / "b", database_root, _Stub(*_script()))
    for key in ("solved", "reason", "sql", "termination", "turns", "tool_calls"):
        assert over_mcp["detail"][key] == over_local["detail"][key], key


@pytest.mark.asyncio
async def test_the_execution_record_does_not_cross_the_wire(database) -> None:
    """**The stated loss, asserted so that nobody finds it by accident.**

    The MCP surface returns a tool's *content* and not its execution record, because a
    structured return would replace the readable text with JSON. `rows_returned`,
    `rows_shown`, `truncated_by` and `elapsed_s` are therefore `None` on this path — and
    **3.4's recovery rate is computed from exactly those fields**, so a transcript produced
    over MCP cannot yield one.

    That is the hard reason 3.6 runs A1 against `LocalTools`, and it is a correctness
    argument rather than a performance one. A future session that flips the default without
    reading this will fail here.
    """
    call = ToolCall(id="c", name="execute_sql", arguments={"sql": "SELECT name FROM singer"})
    local = tools.call_tool(Sandbox(), database, call)
    assert local.rows_returned == 2 and local.elapsed_s is not None

    caller = McpTools()
    try:
        remote = await caller.call(database, call)
    finally:
        await caller.aclose()

    assert remote.content == local.content and remote.ok == local.ok
    assert remote.rows_returned is None
    assert remote.rows_shown is None
    assert remote.truncated_by is None
    assert remote.elapsed_s is None


@pytest.mark.asyncio
async def test_an_unknown_tool_name_is_answered_rather_than_raised(database) -> None:
    """A model that misspells a tool name has made the same kind of mistake as one that
    misspells a column, and raising would end a task over a typo. MCP's own reply to an
    unknown tool is a transport-level error, so this one case is answered client-side — with
    the same sentence `tools.call_tool` uses.
    """
    caller = McpTools()
    try:
        result = await caller.call(database, ToolCall(id="c", name="list_tabels", arguments={}))
    finally:
        await caller.aclose()
    assert not result.ok
    assert "no tool named 'list_tabels'" in result.content
    for name in TOOL_NAMES:
        assert name in result.content


@pytest.mark.asyncio
async def test_closing_the_caller_twice_is_not_a_second_fault(database) -> None:
    """A killed run's cleanup must not raise on its way out."""
    caller = McpTools()
    await caller.call(database, ToolCall(id="c", name="list_tables", arguments={}))
    await caller.aclose()
    await caller.aclose()
