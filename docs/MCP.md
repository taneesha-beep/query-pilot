# The MCP server

The four tools A1 discovers a database with — `list_tables`, `describe_table`, `sample_rows`,
`execute_sql` — exposed over MCP on stdio, so that anything speaking the protocol can list
them and call them. **One implementation, wrapped.** `src/query_pilot/mcp_server.py` contains
no tool logic: each of its four functions calls the corresponding function in
`src/query_pilot/agents/tools.py` and returns the string it produces. A test asserts that what
crosses the wire is character-for-character what the in-process tool returns, which is what
"wrapped, not reimplemented" means when it is checked rather than claimed.

## What it is, in one line each

| | |
|---|---|
| **Server name** | `query-pilot` |
| **Transport** | stdio |
| **Tools** | exactly four, with the descriptions and parameters the model is offered locally |
| **Database** | one, fixed when the process starts, served as a **copy** |
| **Reach** | read-only, `query_only`, a 30-second deadline, a 50,000-row cap and a 1 MiB cap |
| **Dependency** | `mcp==2.2.0`, an **optional extra** — `uv sync --extra mcp` |

**One server, one database.** The tool schemas carry no `db_id` argument, and adding one here
would make the MCP surface advertise a fifth parameter the model is never offered locally. So
the database is chosen on the command line.

## Running it

```bash
uv run --extra mcp python -m query_pilot.mcp_server \
    --database-root data/spider/database --db-id concert_singer
```

`--db-id` goes through `SubstrateCopies`, so the substrate itself is never opened and the
server serves a copy that is deleted when the process exits. `--database <path.sqlite>` serves
a file directly instead, which is what the tests and `agents/mcp_tools.py` use.

## Configuring an external client

Claude Desktop reads `~/Library/Application Support/Claude/claude_desktop_config.json` on
macOS. Add the `query-pilot` entry below, keeping any servers already there.

**Absolute paths throughout, and the interpreter is the project's own `.venv`.** A client
launches a server with no working directory and a minimal `PATH`, so `uv`, a relative path or
a bare `python` will all fail in ways whose error message never reaches you. The block below
was started from an unrelated working directory and verified to list four tools and answer a
query.

```json
{
  "mcpServers": {
    "query-pilot": {
      "command": "/Users/taneeshabadhe/Desktop/resume projects/query-pilot/.venv/bin/python",
      "args": [
        "-m",
        "query_pilot.mcp_server",
        "--database-root",
        "/Users/taneeshabadhe/Desktop/resume projects/query-pilot/data/spider/database",
        "--db-id",
        "concert_singer"
      ]
    }
  }
}
```

`concert_singer` is a **working-set** database — task `dev-0003` used it in run
`20260908-155422-c28177`. `splits/reserve.json` is not read by this server, by this document,
or by anything that configures it.

Restart the client after editing the file. Four tools should appear under `query-pilot`, and
asking it a question about the database should produce a `list_tables`, a `describe_table` and
an `execute_sql`.

### What it looks like when it works

`docs/mcp-claude-desktop.png` is the evidence for 3.5's acceptance: an external client
listing all four tools and successfully querying a sandbox database.

### If it does not appear

- **Nothing listed at all** — the command failed to start. Run the exact `command` and `args`
  from a terminal; the server prints its own errors to stderr, which the client swallows.
- **`No module named mcp`** — the extra is not installed. `uv sync --extra mcp`.
- **`no database 'concert_singer'`** — the substrate is not acquired. See `docs/SUBSTRATE.md`.

## What the tools will and will not do

Every control from 2.2 applies through the wire, because the server adds a transport and no
permissions. **An external client pointed at this has exactly A1's reach and no more** — which
is what makes it a sandbox database rather than a database.

- A write is refused. `DELETE`, `UPDATE`, `INSERT`, `DROP` and `CREATE` are all rejected by the
  connection, and a test asserts the row count is unchanged afterwards. `mode=ro` alone does
  **not** close this — a `mode=ro` connection will happily `ATTACH` a second file and write
  into it, measured on SQLite 3.53.1 — so `PRAGMA query_only = ON` is set as well.
- A slow statement is interrupted at 30 seconds; a large result is truncated at 50,000 rows or
  1 MiB and **says so**, so a capped result can never read as a complete one.
- An error is **returned, not raised**: bad SQL, an unknown table and a wrong argument all come
  back as text beginning `ERROR: `, because a model that can read an error can fix it.
- Results are rendered under the caps A1's prompt is built to: 10 rows of a query, 5 sampled
  rows by default, 120 characters a value, 6,000 characters a whole result. `docs/a1-tool-sizes.json`
  is where those come from.

## A1 over MCP, and why the measured run is not

`A1(..., tools=McpTools())` makes the agent a client of this server, and
`tests/test_mcp.py::test_a1_solves_a_whole_task_over_mcp` drives a whole task through it —
every tool call a JSON-RPC round trip to a subprocess.

**3.6 does not run that way**, and the reason is not process cost:

> The MCP surface returns a tool's **content** and not its **execution record**. A structured
> return would replace the readable grid with JSON, so `rows_returned`, `rows_shown`,
> `truncated_by` and `elapsed_s` do not cross the wire. **3.4's recovery rate is computed from
> exactly those fields**, out of the transcript's `tool_result` events — so a trajectory driven
> over MCP cannot produce one.

That is a correctness argument, and a test asserts the loss so that nobody discovers it by
accident. The second reason is smaller and still real: a dead server would be a new failure
mode inside the measured run, whose honest classification is `executor_error` — this project's
own fault, the label reserved for a substrate that will not open.

So the roadmap's "the agent becomes one client of that server" is satisfied as a **capability,
proven by a test**, rather than as the default. That is a stated deviation.

## What the dependency costs

`mcp==2.2.0` resolves to **28 packages**, against this project's one runtime dependency
(`httpx`). Among them: `cryptography`, `pydantic`, `starlette`, `uvicorn`,
`opentelemetry-api`, `pyjwt`, and `httpx2` — a *second* HTTP client beside the one
`client/` uses.

That is why it is an **optional extra** rather than a runtime dependency: `pip install
query-pilot` still installs `httpx` and nothing else, and the measured path never imports
`mcp`. It is also in the dev group, so CI runs `tests/test_mcp.py`; where the extra is absent
those tests skip rather than fail.

**Nothing in `mcp` touches the network at import time** — checked by importing it with
`socket.socket.connect` replaced by a raise, which took 0.33 seconds and did not fire. The
test suite still runs with no network and no keys, which is constraint 5.
