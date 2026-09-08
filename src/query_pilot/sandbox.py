"""Where generated SQL is allowed to run, and what it is not allowed to do there.

Every query this project executes — the model's and the reference's alike — goes through
:class:`Sandbox`. It opens a connection that cannot write, stops a query that will not
finish, caps what a query may return, and executes against a **copy** so that the executor
never has the substrate open at all.

**Standard library only.** It imports neither `query_pilot.client` nor `query_pilot.run`,
which are general infrastructure with no knowledge of SQL, and it does not import
`query_pilot.equivalence` either: the sandbox reports *what came back*, the rule decides
*whether it is the same answer*, and the agent is the one layer that knows both. Coupling
the measurement instrument to the executor would give the rule an opinion about execution,
which is the thing 2.1 was written before 2.2 to avoid.

**This is not the statement filter.** Phase 4.1's five controls include rejecting DDL, DML
and multi-statement input *before* execution. Those sit above this module and are a
different layer: what is here is what the connection itself enforces, which holds even
against a statement no filter anticipated.

Four controls, and the fourth is not in the roadmap's list because it was found by
measuring rather than by reading:

1. **Read-only** — `file:...?mode=ro`. A write raises rather than happening.
2. **`PRAGMA query_only = ON`** — because `mode=ro` binds the *main* database and nothing
   else. A `mode=ro` connection will happily `ATTACH` a second file and write into it;
   measured on SQLite 3.53.1, 2026-09-08. `query_only` is what closes that, and the copy
   does not.
3. **A statement deadline**, checked from a progress handler.
4. **A row cap and a byte cap**, applied as rows arrive.

And the copy underneath all four, so that a bug in any of them still cannot reach
`data/spider`.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

__all__ = [
    "BYTE_CAP",
    "PROGRESS_INTERVAL",
    "ROW_CAP",
    "STATEMENT_TIMEOUT_S",
    "TRUNCATED_BY_BYTES",
    "TRUNCATED_BY_ROWS",
    "CopyScope",
    "Sandbox",
    "SandboxResult",
    "SubstrateCopies",
    "database_path",
    "encoded_size",
    "has_statement",
    "result_size",
]

#: The most rows a query may return before the rest are dropped and the result is marked
#: truncated.
#:
#: A policy choice, like the equivalence rule's float tolerance, and derived rather than
#: picked. The largest reference result anywhere in the 1,034-task frame is **20,662 rows**
#: (`dev-0456`, on `wta_1`), measured by `scripts/sandbox_caps.py` and recorded in
#: `docs/sandbox-caps.json`. 50,000 sits 2.4x above it, so **no reference
#: result in this frame is truncated by this cap** — which is the whole requirement. A cap
#: that can decide a legitimate answer is a cap that silently moves the accuracy figure,
#: and `equivalence.compare` refuses to compare a truncated result precisely so that it
#: cannot be mistaken for a wrong one.
ROW_CAP: Final = 50_000

#: The most result bytes a query may return before the rest are dropped.
#:
#: Derived the same way: the largest reference result in the frame encodes to **289,104
#: bytes**, and 1 MiB sits 3.6x above it, so no reference result in this frame is truncated
#: by this cap either. It bounds the other pathology the row cap does not — few rows
#: holding very large values.
#:
#: **What a byte is here** is :func:`encoded_size`, and it is stated precisely because 2.4
#: will quote this number: the size of the *values*, not of the Python objects holding them
#: and not of any serialisation of them.
BYTE_CAP: Final = 1_048_576

#: How long one statement may run, in seconds, measured from the moment it is submitted and
#: covering the fetch as well as the first step.
#:
#: Derived from two ends. The slowest reference query in the whole frame runs in **0.2828 s**
#: (`dev-0472`, on `wta_1`), so 30 s is 106x the slowest thing this substrate legitimately
#: asks for. And 150 tasks x 30 s is 4,500 s against the working set's declared 14,400 s
#: wall-clock ceiling, so a run in which *every* candidate hangs still cannot be what trips
#: that ceiling.
#:
#: **What it cannot do, stated rather than discovered in 2.4:** a semantically *correct*
#: reformulation can be pathologically slow. A correlated-subquery form of `dev-0471`
#: returning the same rows was measured at over 60 s against that reference's 0.289 s. No
#: timeout value saves that query, so the timeout necessarily costs some correct answers —
#: another of the reasons execution accuracy here is a lower bound. What the timeout is
#: worth is the alternative: a run that stops.
STATEMENT_TIMEOUT_S: Final = 30.0

#: How many SQLite virtual-machine instructions run between deadline checks.
#:
#: **The deadline is wall-clock; the sampling is not.** SQLite has no wall-clock timeout, so
#: this is a progress handler that reads the clock. Measured on this machine at roughly
#: 3.0e8 VM steps a second, 10,000 steps is about 33 microseconds between checks — three
#: orders of magnitude finer than the deadline — and its overhead was inside run-to-run
#: noise (2.615 s against 2.609 s unhandled over the same query), where 1,000 steps cost a
#: measurable ~3%.
#:
#: The limit this leaves: a single VM instruction that does not return is not interrupted.
#: Nothing in this substrate does that, and the alternative — `Connection.interrupt` from a
#: watchdog thread — has the same limit for the same reason.
PROGRESS_INTERVAL: Final = 10_000

TRUNCATED_BY_ROWS: Final = "rows"
TRUNCATED_BY_BYTES: Final = "bytes"


_LITERAL_OR_COMMENT = re.compile(
    r"'(?:[^']|'')*'"  # a string literal, with '' as an escaped quote
    r"|\"(?:[^\"]|\"\")*\""  # a double-quoted identifier
    r"|`[^`]*`|\[[^\]]*\]"  # MySQL and T-SQL quoting, which SQLite also accepts
    r"|(--[^\n]*|/\*.*?\*/)",  # the group that matters: a comment
    re.DOTALL,
)


def has_statement(sql: str) -> bool:
    """Is there anything here to execute, once comments and whitespace are removed?

    **The trap this closes is specific and expensive.** sqlite3 executes `''`, `'   '`,
    `'-- nothing'` and `'/* nothing */'` happily and returns **zero rows** with no error.
    49 of the frame's 1,034 reference queries return no rows, and an empty result matching
    an empty reference is a *solve* — so a model that answered with a comment, or with
    nothing this project could parse, would silently score 4.7389% of the frame as correct
    answers. Naming it an error is what stops that.

    Comments are removed **literal-aware**, so `SELECT '-- not a comment'` still counts as a
    statement. This is not Phase 4.1's statement filter: that decides *which kinds* of
    statement are permitted, and this only answers whether there is one at all.
    """
    return bool(_LITERAL_OR_COMMENT.sub(lambda m: "" if m.group(1) else " ", sql).strip())


def encoded_size(value: Any) -> int:
    """How many bytes one result value counts for, against :data:`BYTE_CAP`.

    Named precisely because 2.4 quotes the cap and a reader has to know what it bounds:

    - **TEXT** — the length of its UTF-8 encoding, so a multi-byte character costs what it
      costs rather than counting as one.
    - **BLOB** — its length.
    - **INTEGER** and **REAL** — 8, which is what SQLite stores them as.
    - **NULL** — 0.

    Deliberately *not* ``sys.getsizeof``, which measures CPython's object layout and would
    make the cap depend on the interpreter, and deliberately not the length of a JSON or
    repr encoding, which would make it depend on a serialiser this module does not own.
    """
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="replace"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    return 8


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """What came back, and what stopped it coming back.

    Errors are **returned, not raised**. A model writing SQL that does not execute is an
    ordinary outcome that the run must count, not an exception that ends a task — and
    `equivalence.compare` takes the message as ``candidate_error`` and files it as a
    non-solve. The caller decides which side of a comparison a failure belongs to, which is
    what keeps a substrate fault from ever being charged to the model.
    """

    rows: tuple[tuple[Any, ...], ...] = ()
    truncated_by: str | None = None
    error: str | None = None
    timed_out: bool = False
    result_bytes: int = 0
    elapsed_s: float = 0.0

    @property
    def truncated(self) -> bool:
        """Pass this straight to ``equivalence.compare``'s ``*_truncated`` argument.

        There is one truncation mechanism in this project and this is it. A capped result
        is not a wrong answer and not a right one; the rule refuses to compare it.
        """
        return self.truncated_by is not None

    @property
    def ok(self) -> bool:
        return self.error is None


class Sandbox:
    """Executes one statement against one database file, under every control at once.

    **A connection per call**, opened and closed inside :meth:`execute`. On the largest
    database in this substrate that costs a measured 49 microseconds, which buys two
    things worth far more: the object is safe to call from a worker thread — and it has to
    be, because a 30-second blocking ``execute`` on the event loop would stall every other
    task in the run — and no state survives one statement to affect the next.
    """

    def __init__(
        self,
        *,
        timeout_s: float = STATEMENT_TIMEOUT_S,
        row_cap: int = ROW_CAP,
        byte_cap: int = BYTE_CAP,
        progress_interval: int = PROGRESS_INTERVAL,
    ) -> None:
        if timeout_s <= 0 or row_cap <= 0 or byte_cap <= 0 or progress_interval <= 0:
            raise ValueError("every sandbox limit must be positive")
        self.timeout_s = timeout_s
        self.row_cap = row_cap
        self.byte_cap = byte_cap
        self.progress_interval = progress_interval

    @contextmanager
    def connect(self, database: Path | str) -> Iterator[sqlite3.Connection]:
        """A connection that cannot write and decodes what this substrate actually holds.

        ``mode=ro`` refuses a write to the main database. ``query_only`` refuses a write to
        anything, including a database this statement attached itself — which ``mode=ro``
        permits and which nothing else here would catch.

        The text factory is the one 0.2 and 2.1 already used, and it belongs to the
        connection rather than to the equivalence rule: several Spider databases hold bytes
        that are not valid UTF-8, both sides of every comparison are decoded the same way,
        and so a decode fault is never read as a wrong answer.
        """
        conn = sqlite3.connect(f"file:{Path(database)}?mode=ro", uri=True)
        try:
            conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
            conn.execute("PRAGMA query_only = ON")
            yield conn
        finally:
            conn.close()

    def execute(self, database: Path | str, sql: str) -> SandboxResult:
        """Run one statement and return what it produced, or why it produced nothing."""
        if not has_statement(sql):
            return SandboxResult(error="no statement to execute")

        started = time.perf_counter()
        deadline = time.monotonic() + self.timeout_s
        expired = False

        def watch() -> int:
            """Called by SQLite every ``progress_interval`` VM steps. Non-zero aborts."""
            nonlocal expired
            if time.monotonic() >= deadline:
                expired = True
                return 1
            return 0

        rows: list[tuple[Any, ...]] = []
        total = 0
        truncated_by: str | None = None
        try:
            with self.connect(database) as conn:
                conn.set_progress_handler(watch, self.progress_interval)
                cursor = conn.execute(sql)
                # Both caps are checked *before* a row is kept, so `truncated` always means
                # at least one row was actually dropped and `result_bytes` never exceeds
                # the cap it is measured against. A result that merely reaches a cap
                # exactly is not truncated, because nothing was lost.
                for row in cursor:
                    if len(rows) >= self.row_cap:
                        truncated_by = TRUNCATED_BY_ROWS
                        break
                    size = sum(encoded_size(value) for value in row)
                    if total + size > self.byte_cap:
                        truncated_by = TRUNCATED_BY_BYTES
                        break
                    total += size
                    rows.append(tuple(row))
        except (sqlite3.Error, sqlite3.Warning) as exc:
            # `sqlite3.Warning` is not a subclass of `sqlite3.Error` and is named here for
            # that reason alone; Python 3.12 raises `ProgrammingError` for the
            # multi-statement case that used to raise it.
            return SandboxResult(
                error="statement timed out" if expired else str(exc),
                timed_out=expired,
                elapsed_s=time.perf_counter() - started,
            )
        return SandboxResult(
            rows=tuple(rows),
            truncated_by=truncated_by,
            result_bytes=total,
            elapsed_s=time.perf_counter() - started,
        )


# --- the copy underneath -------------------------------------------------------------------


class CopyScope(StrEnum):
    """How often the substrate is copied, and what each choice buys.

    ``RUN`` — one copy of each database, shared by every task in the run. **The default.**
    Measured on the 150-task working set: 20 databases, 105.7 MB, about 0.2 s. Every
    connection is read-only, so no task can affect another task's view of a shared copy,
    and this is therefore the same isolation as ``TASK`` at a fifth of the cost.

    ``TASK`` — a fresh copy per task, removed when the task ends. Measured on the same run:
    0.8 GB and about 1 s. It buys nothing today and everything once a copy is opened
    writable, which is what Phase 4.2's attack databases need: one attack case's mutation
    must not be visible to the next.

    The measurement that made this a choice rather than an obligation: the substrate is
    879 MB, but `soccer_1` at 626 MB is a **train** database and is in no split this
    project runs. The 1,034-task frame touches 20 databases totalling 105.7 MB.
    """

    RUN = "run"
    TASK = "task"


def database_path(database_root: Path | str, db_id: str) -> Path:
    """Spider's layout: ``<database_root>/<db_id>/<db_id>.sqlite``.

    One function rather than the same join written at four call sites, so that a substrate
    laid out differently is one edit. `docs/SUBSTRATE.md` is what fixes this shape.
    """
    return Path(database_root) / db_id / f"{db_id}.sqlite"


#: Copied alongside the database file when they exist. **None exist in this substrate** —
#: checked over all 166 directories — but a database copied without its write-ahead log
#: silently loses whatever the log had not yet checkpointed, and finding that out from a
#: wrong answer would be expensive.
SIDECAR_SUFFIXES: Final = ("-wal", "-shm", "-journal")


class SubstrateCopies:
    """Hands out paths to copies, so the executor never opens `data/spider` at all.

    Read-only connections already make the substrate safe from this project's own
    execution. This is the layer under that: it holds whether a control is correct, and it
    is the mechanism Phase 4.2 needs when a database has to be opened writable. The copies
    live in a temporary directory and :meth:`close` removes all of them.

    Safe to use from several worker threads, which it has to be at the run's concurrency
    of 8.
    """

    def __init__(
        self,
        database_root: Path | str,
        *,
        scope: CopyScope = CopyScope.RUN,
        parent: Path | str | None = None,
    ) -> None:
        self.database_root = Path(database_root)
        self.scope = CopyScope(scope)
        self.root = Path(tempfile.mkdtemp(prefix="query-pilot-", dir=parent))
        self._shared: dict[str, Path] = {}
        self._lock = threading.Lock()

    def __enter__(self) -> SubstrateCopies:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Remove every copy. Idempotent, so a killed run's cleanup is not a second fault."""
        shutil.rmtree(self.root, ignore_errors=True)
        with self._lock:
            self._shared.clear()

    @contextmanager
    def for_task(self, db_id: str, *, task_id: str) -> Iterator[Path]:
        """The database this task should execute against.

        Under ``RUN`` the copy is made once and outlives the task; under ``TASK`` it is made
        here and removed on the way out. One call site covers both, which is what keeps a
        scope change from being a change to every caller.
        """
        if self.scope is CopyScope.RUN:
            yield self._shared_copy(db_id)
            return
        destination = self.root / task_id / f"{db_id}.sqlite"
        self._copy(db_id, destination)
        try:
            yield destination
        finally:
            shutil.rmtree(destination.parent, ignore_errors=True)

    def _shared_copy(self, db_id: str) -> Path:
        with self._lock:
            existing = self._shared.get(db_id)
            if existing is not None:
                return existing
            destination = self.root / CopyScope.RUN / f"{db_id}.sqlite"
            self._copy(db_id, destination)
            self._shared[db_id] = destination
            return destination

    def _copy(self, db_id: str, destination: Path) -> None:
        source = database_path(self.database_root, db_id)
        if not source.exists():
            raise FileNotFoundError(f"no database {db_id!r} under {self.database_root}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        for suffix in SIDECAR_SUFFIXES:
            sidecar = source.with_name(source.name + suffix)
            if sidecar.exists():
                shutil.copyfile(sidecar, destination.with_name(destination.name + suffix))


def result_size(rows: Sequence[Sequence[Any]]) -> int:
    """Total :func:`encoded_size` over a whole result. What the byte cap counts.

    Here rather than only inside :meth:`Sandbox.execute` so that
    `scripts/sandbox_caps.py` measures the caps with the same arithmetic the caps enforce.
    A derivation taken with a second implementation is a derivation of something else.
    """
    return sum(encoded_size(value) for row in rows for value in row)
