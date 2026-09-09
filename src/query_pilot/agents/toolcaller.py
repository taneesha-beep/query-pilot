"""How A1 reaches the four tools. **Two ways, and the default is the direct one.**

3.5 asks that the agent become one client of the MCP server rather than calling the tools
directly. This is the seam that makes that true, and it is one interface with two
implementations over **one** set of tool functions:

:class:`LocalTools`  calls :func:`query_pilot.agents.tools.call_tool` in this process. The
                     default, and what 3.6's measured run uses.
:class:`~query_pilot.agents.mcp_tools.McpTools`  speaks MCP over stdio to
                     :mod:`query_pilot.mcp_server`, which wraps the same four functions.

**Why the default is local, stated as a deviation rather than slipped in.** Over stdio,
3.6's 150 tasks would pay a subprocess and a JSON-RPC round trip per tool call for **no
possible change in any answer** — the tools are the same functions either way. Against that,
two costs it would actually incur:

1. **A dead server becomes a new failure mode inside the measured run**, with no existing
   error class. Its honest classification is `executor_error`, this project's own fault
   (1.3), which is the label 2.5's taxonomy reserves for a substrate that will not open.
2. **The execution record does not survive the wire.** The MCP surface returns
   ``ToolResult.content`` — the characters the model reads — and not ``rows_returned``,
   ``rows_shown``, ``truncated_by`` or ``elapsed_s``. **3.4's recovery rate is computed from
   exactly those fields**, so a transcript produced over MCP cannot yield one. That is a
   correctness argument rather than a performance one, and it is the reason this default is
   not merely a preference.

So A1 *is* a client of the server — provably, by a test that spawns the real server over
stdio and drives a whole task through it — and 3.6 does not pay for it. The roadmap's
sentence is satisfied by capability rather than by default, which is the deviation.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from query_pilot.agents.tools import ToolResult, call_tool
from query_pilot.client.types import ToolCall
from query_pilot.sandbox import Sandbox

__all__ = ["LocalTools", "ToolCaller"]


@runtime_checkable
class ToolCaller(Protocol):
    """One tool call, executed somewhere. **Async because one implementation has to be.**

    The local path does no waiting and would be happy synchronous; the MCP path is a round
    trip over a pipe. Making the seam async costs the local path nothing measurable — an
    awaited coroutine that runs synchronous code runs it synchronously — and making it sync
    would have forced the MCP path to drive its own event loop inside A1's.
    """

    async def call(self, database: Path, call: ToolCall) -> ToolResult:
        """Run one call against one database and return what it produced.

        **Never raises for anything the model did.** A bad argument, an unknown table, SQL
        that does not run and an unknown tool name all come back as a :class:`ToolResult`
        with ``error`` set, because the loop answers those and that is what makes recovery
        measurable at all. What may raise is a failure of the mechanism itself — a substrate
        that will not open, a server that died — which 1.3 files as `executor_error`.
        """
        ...

    async def aclose(self) -> None:
        """Release whatever this holds. Idempotent, so a killed run's cleanup is not a
        second fault."""
        ...


class LocalTools:
    """The four functions, called in this process. **The default, and 3.6's path.**

    Holds the lock A1 used to hold. A run may drive several tasks at once and the copies are
    shared under `CopyScope.RUN`; the sandbox opens a connection per call, so this is not
    protecting sqlite objects across threads so much as keeping one call's work whole.
    """

    def __init__(self, sandbox: Sandbox | None = None) -> None:
        self.sandbox = sandbox or Sandbox()
        self._lock = threading.Lock()

    async def call(self, database: Path, call: ToolCall) -> ToolResult:
        with self._lock:
            return call_tool(self.sandbox, database, call)

    async def aclose(self) -> None:
        """Nothing to release. Defined so the two implementations are interchangeable."""
