"""A1 as a client of the MCP server. **The other implementation of the seam.**

    agent = A1(client, tasks, root, run_directory, tools=McpTools())

One server process per database, started on the first call for that database and kept for as
long as this object lives. Per database rather than per task because under `CopyScope.RUN`
the copy is shared by every task that touches it, so a server per task would be up to 150
processes serving 20 files.

**This is not what 3.6 runs**, and :mod:`query_pilot.agents.toolcaller` says why in full. The
short reason is not process cost: the MCP surface returns the tool's *content* and not its
execution record, so ``rows_returned``, ``rows_shown``, ``truncated_by`` and ``elapsed_s``
are ``None`` on this path — and 3.4's recovery rate is computed from exactly those. A
transcript produced over MCP cannot yield one. :func:`~query_pilot.agents.tools.ToolResult`'s
``ERROR:`` prefix is what survives, and it is enough to reconstruct ``ok``.

**What a dead server does.** Nothing here catches a transport failure and turns it into a
tool error. A tool error is something the model can act on and this is not: the model did not
kill the server. It propagates, the run loop records the task **failed**, 1.3 files it as
`executor_error` — the same treatment a substrate that will not open gets — and a resume
retries it. That is the honest classification, and it is a failure mode the local path simply
does not have.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Final

from query_pilot.agents.tools import ToolResult
from query_pilot.client.types import ToolCall

__all__ = ["ERROR_PREFIX", "McpTools", "server_command"]

#: How a failed tool result is spelled by :meth:`ToolResult.failed`. Load-bearing on this
#: path in a way it is not locally: it is the only thing left of ``error`` after the trip.
ERROR_PREFIX: Final = "ERROR: "


def server_command(database: Path) -> list[str]:
    """The argv that starts a server for one database. **One place, so `docs/MCP.md` and the
    tests cannot disagree about it.**"""
    return [sys.executable, "-m", "query_pilot.mcp_server", "--database", str(database)]


class McpTools:
    """Speaks MCP over stdio to :mod:`query_pilot.mcp_server`. One session per database.

    **Each session lives inside its own task, and that is not a style choice.** The stdio
    client is built on anyio cancel scopes, which must be entered and exited by the *same*
    task — and a run drives its tasks in workers, so the worker that makes the first tool
    call for a database is almost never the one that shuts the agent down. Opening the
    session in the caller's task and closing it in another raises
    ``Attempted to exit cancel scope in a different task than it was entered in``, which is
    what the first version of this did. So one task owns the whole lifetime of a session and
    every caller talks to it through a queue.

    The alternative — a fresh subprocess per tool call, entered and exited in the caller —
    is also correct and was rejected: it would put a process spawn on every call of every
    trajectory, which is a large cost for a path that exists to be demonstrated.
    """

    def __init__(self) -> None:
        self._workers: dict[str, _Session] = {}
        self._lock = asyncio.Lock()

    async def _session(self, database: Path) -> _Session:
        key = str(database)
        async with self._lock:
            existing = self._workers.get(key)
            if existing is None:
                existing = _Session(database)
                await existing.start()
                self._workers[key] = existing
            return existing

    async def call(self, database: Path, call: ToolCall) -> ToolResult:
        """One call, over the wire. **An unknown tool name is still answered, not raised.**

        The server does not know the four names A1 might invent, and MCP's own reply to an
        unknown tool is an error rather than a structured result — so that one case is
        answered here, with the same sentence `tools.call_tool` uses, rather than being
        allowed to end a task over a typo.
        """
        from query_pilot.agents.tools import TOOL_NAMES

        if call.name not in TOOL_NAMES:
            return ToolResult.failed(
                f"no tool named {call.name!r}. The tools are: {', '.join(TOOL_NAMES)}"
            )
        session = await self._session(database)
        result = await session.call(call.name, dict(call.arguments))
        content = "\n".join(
            block.text for block in result.content if getattr(block, "text", None) is not None
        )
        # The execution record did not cross the wire; only the prefix did. Reconstructing
        # `error` from it is what keeps `ToolResult.ok` meaning the same thing on both paths.
        error = content[len(ERROR_PREFIX) :] if content.startswith(ERROR_PREFIX) else None
        if result.is_error and error is None:
            error = content or "the tool call failed"
        return ToolResult(content=content, error=error)

    async def aclose(self) -> None:
        """Stop every server. Idempotent, so a killed run's cleanup is not a second fault."""
        async with self._lock:
            workers, self._workers = self._workers, {}
        for worker in workers.values():
            await worker.aclose()


class _Session:
    """One server subprocess, owned end to end by one task. See :class:`McpTools`."""

    def __init__(self, database: Path) -> None:
        self.database = database
        self._requests: asyncio.Queue[Any] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._serve(ready))
        # Propagates a server that would not start -- a missing extra, a database that will
        # not open -- as this project's own fault rather than as a tool error. 1.3 files it
        # as `executor_error`, the same as a substrate that will not open.
        await ready

    async def _serve(self, ready: asyncio.Future[None]) -> None:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        argv = server_command(self.database)
        try:
            parameters = StdioServerParameters(command=argv[0], args=argv[1:])
            async with (
                stdio_client(parameters) as (reader, writer),
                ClientSession(reader, writer) as session,
            ):
                await session.initialize()
                ready.set_result(None)
                while True:
                    item = await self._requests.get()
                    if item is None:
                        return
                    name, arguments, answer = item
                    try:
                        answer.set_result(await session.call_tool(name, arguments))
                    except BaseException as error:
                        # A failure of one call is that caller's, not the session's: the
                        # server is still up and the next call must still be servable.
                        answer.set_exception(error)
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            else:
                # The server died mid-trajectory. Every pending caller is told, rather than
                # left awaiting a future nothing will ever complete.
                while not self._requests.empty():
                    item = self._requests.get_nowait()
                    if item is not None and not item[2].done():
                        item[2].set_exception(error)

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        answer: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._requests.put((name, arguments, answer))
        return await answer

    async def aclose(self) -> None:
        if self._task is None:
            return
        task, self._task = self._task, None
        await self._requests.put(None)
        await asyncio.wait_for(task, timeout=10)
