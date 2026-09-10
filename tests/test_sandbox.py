"""What the sandbox refuses, what it caps, and what it stops.

**No substrate and no network.** Every database here is built by the fixtures below, so the
suite says the same thing on a machine that has never run `docs/SUBSTRATE.md`. The two
tests that need the real frame's numbers read `docs/sandbox-caps.json`, which is committed
for exactly that reason — the cap values are policy choices and CI has to be able to check
that they still sit above what the substrate legitimately returns.

The one test that reaches outside this module is the truncation seam: a capped result has
to arrive at `equivalence.compare` as `truncated` and not as a wrong answer, and asserting
that inside one module would only prove that module agrees with itself.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from query_pilot import sandbox
from query_pilot.equivalence import TRUNCATED, compare
from query_pilot.sandbox import (
    BYTE_CAP,
    PROGRESS_INTERVAL,
    ROW_CAP,
    STATEMENT_TIMEOUT_S,
    TRUNCATED_BY_BYTES,
    TRUNCATED_BY_ROWS,
    CopyScope,
    Sandbox,
    SubstrateCopies,
    database_path,
    encoded_size,
    result_size,
    single_read_only_statement,
)

CAPS_REPORT = Path(__file__).resolve().parent.parent / "docs" / "sandbox-caps.json"

#: Two forms of "this will not finish", and they are stopped by different controls.
#:
#: The CTE computes for ever and returns one row, so no cap can reach it and only the
#: deadline can. The cross join returns 64,000,000 rows, so under the real limits it is a
#: cap that stops it and the deadline is never approached. That the two swap places is the
#: argument for having both, and it is asserted below rather than left as a claim.
INFINITE_CTE = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
)
RUNAWAY_CROSS_JOIN = "SELECT a.n, b.n, c.n FROM wide a, wide b, wide c"


@pytest.fixture
def database_root(tmp_path: Path) -> Path:
    """A substrate-shaped directory: `<root>/<db_id>/<db_id>.sqlite`."""
    root = tmp_path / "database"
    _build(root, "singer", _singer)
    _build(root, "wide", _wide)
    _build(root, "spare", _spare)
    return root


def _build(root: Path, db_id: str, populate) -> Path:
    path = database_path(root, db_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        populate(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _singer(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE singer (name TEXT, age INTEGER, country TEXT)")
    conn.executemany(
        "INSERT INTO singer VALUES (?, ?, ?)",
        [("Joe", 30, "France"), ("Rose", 41, None), ("Tribal King", 25, "France")],
    )


def _wide(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE wide (n INTEGER, blob TEXT)")
    conn.executemany("INSERT INTO wide VALUES (?, ?)", [(n, "x" * 100) for n in range(400)])


def _spare(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE spare (n INTEGER)")


@pytest.fixture
def singer(database_root: Path) -> Path:
    return database_path(database_root, "singer")


@pytest.fixture
def wide(database_root: Path) -> Path:
    return database_path(database_root, "wide")


# --- the connection cannot write ----------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO singer VALUES ('Nobody', 1, 'X')",
        "UPDATE singer SET age = 0",
        "DELETE FROM singer",
        "CREATE TABLE planted (a)",
        "DROP TABLE singer",
        "ALTER TABLE singer ADD COLUMN planted TEXT",
        "CREATE INDEX ix ON singer (name)",
    ],
)
def test_a_write_is_rejected_and_the_database_is_unchanged(singer: Path, statement: str) -> None:
    before = singer.read_bytes()
    result = Sandbox().execute(singer, statement)

    assert not result.ok
    assert "readonly" in result.error
    assert not result.timed_out
    assert singer.read_bytes() == before


def test_a_rejected_write_is_returned_rather_than_raised(singer: Path) -> None:
    """A model writing SQL that cannot run is an outcome to count, not an exception.

    `equivalence.compare` takes the message as `candidate_error` and files it as a
    non-solve. A sandbox that raised would make one bad statement end a task, and the
    ledger would record a failed task where the run in fact got an answer.
    """
    result = Sandbox().execute(singer, "DROP TABLE singer")

    verdict = compare([("Joe",)], None, ordered=False, candidate_error=result.error)
    assert verdict.reason == "candidate_error"
    assert not verdict.solved


def test_mode_ro_alone_would_let_an_attached_database_be_written(
    singer: Path, database_root: Path
) -> None:
    """The negative control for `PRAGMA query_only`, and the reason it is there.

    `mode=ro` binds the **main** database and nothing else. This test opens the connection
    the roadmap's 2.2 text describes — read-only and nothing more — and writes through it,
    which is why the sandbox does not stop at that line. If a future SQLite makes this
    impossible, this test fails and the pragma becomes belt-and-braces rather than the
    control it currently is.
    """
    spare = database_path(database_root, "spare")
    conn = sqlite3.connect(f"file:{singer}?mode=ro", uri=True)
    try:
        conn.execute(f"ATTACH DATABASE '{spare}' AS other")
        conn.execute("INSERT INTO other.spare VALUES (1)")
        conn.commit()
    finally:
        conn.close()

    check = sqlite3.connect(spare)
    try:
        assert check.execute("SELECT count(*) FROM spare").fetchone() == (1,)
    finally:
        check.close()


def test_query_only_stops_a_write_to_a_database_the_statement_attached(
    singer: Path, database_root: Path
) -> None:
    spare = database_path(database_root, "spare")
    box = Sandbox()

    with box.connect(singer) as conn:
        conn.execute(f"ATTACH DATABASE '{spare}' AS other")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO other.spare VALUES (1)")

    check = sqlite3.connect(spare)
    try:
        assert check.execute("SELECT count(*) FROM spare").fetchone() == (0,)
    finally:
        check.close()


def test_a_read_still_works(singer: Path) -> None:
    result = Sandbox().execute(singer, "SELECT name, age FROM singer ORDER BY age")

    assert result.ok
    assert result.rows == (("Tribal King", 25), ("Joe", 30), ("Rose", 41))
    assert not result.truncated
    assert result.result_bytes == result_size(result.rows)


# --- the five controls of 4.1, one named test each ---------------------------------------
#
# These five are the acceptance tests Phase 4.1 owes: one per control, named after the
# control it proves. The finer-grained tests around them (the `mode=ro`/`query_only` split,
# the two forms of runaway, the cap arithmetic) stay because they prove *why* each control is
# shaped the way it is; these five prove *that* each one fires.


def test_control_read_only_connection_rejects_a_write(singer: Path) -> None:
    """#1 — a write is refused at the connection, and the database is left unchanged."""
    before = singer.read_bytes()
    result = Sandbox().execute(singer, "DELETE FROM singer")

    assert not result.ok
    assert "readonly" in (result.error or "")
    assert singer.read_bytes() == before


def test_control_statement_timeout_interrupts_a_slow_query(wide: Path) -> None:
    """#2 — a query that will not finish is stopped by the deadline, not left to run."""
    result = Sandbox(timeout_s=0.25).execute(wide, INFINITE_CTE)

    assert not result.ok
    assert result.timed_out
    assert result.error == "statement timed out"


def test_control_caps_truncate_and_flag_a_large_result(wide: Path) -> None:
    """#3 — a result past the cap comes back truncated and says so, never silently short."""
    result = Sandbox(row_cap=10).execute(wide, "SELECT n FROM wide")

    assert result.ok
    assert result.truncated
    assert result.truncated_by == TRUNCATED_BY_ROWS
    assert len(result.rows) == 10


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE singer",
        "DELETE FROM singer",
        "UPDATE singer SET age = 0",
        "INSERT INTO singer VALUES ('x', 1, 'y')",
        "ALTER TABLE singer ADD COLUMN planted TEXT",
        "ATTACH DATABASE 'elsewhere.db' AS other",
        "PRAGMA writable_schema = ON",
    ],
)
def test_control_ddl_dml_is_rejected_before_execution(statement: str) -> None:
    """#4 — DDL, DML, PRAGMA and ATTACH are refused before a connection is opened.

    Proven "before execution" by pointing the guard at a database that does not exist: a
    statement that reached execution would fail on the missing file with "unable to open
    database file", and the guard's own reason is what comes back instead.
    """
    missing = Path("/nonexistent/does-not-exist.sqlite")
    result = Sandbox().execute_guarded(missing, statement)

    assert not result.ok
    assert "not a read-only query" in (result.error or "")
    assert "unable to open" not in (result.error or "")


def test_control_multi_statement_is_rejected(singer: Path) -> None:
    """#5 — a `;`-separated batch is refused as one unit, before execution.

    The same missing-database proof of timing: the guard answers before the file is touched,
    and its message names the one-statement rule rather than the driver's own mid-batch
    error. `execute` alone would also refuse this (SQLite raises "one statement at a time"),
    but only after opening the connection and only for a caller that reached `execute`.
    """
    missing = Path("/nonexistent/does-not-exist.sqlite")
    result = Sandbox().execute_guarded(missing, "SELECT 1; DROP TABLE singer")

    assert not result.ok
    assert "one statement" in (result.error or "")
    assert "unable to open" not in (result.error or "")


def test_the_guard_admits_the_reads_the_tools_actually_make(singer: Path) -> None:
    """The guard's whitelist must not refuse a legitimate query, or it moves the number.

    A plain SELECT, a compound query behind parentheses, and a CTE all open a read-only
    query and must be admitted; a semicolon inside a literal is a value, not a separator.
    This is the false-rejection direction of controls 4 and 5 — the one that would silently
    cost A1 a solved task if it were wrong.

    **Admitting a statement is not the same as SQLite running it.** The guard's job is the
    shape; `(SELECT ...) UNION (SELECT ...)` is a shape SQLite's own grammar happens to
    reject, and `agents.validate` admits it for the same reason — the sandbox then reports a
    `candidate_error`, which is the honest outcome and not the guard's to pre-empt. So the
    whitelist is checked on every form, and execution only on the forms SQLite parses.
    """
    admitted = [
        "SELECT name FROM singer",
        "(SELECT name FROM singer) UNION (SELECT country FROM singer)",
        "WITH ages AS (SELECT age FROM singer) SELECT max(age) FROM ages",
        "SELECT 'a; b' AS literal",
    ]
    for sql in admitted:
        assert single_read_only_statement(sql) is None, sql

    runnable = [sql for sql in admitted if "UNION" not in sql]
    for sql in runnable:
        assert Sandbox().execute_guarded(singer, sql).ok, sql


# --- the statement deadline -----------------------------------------------------------------


def test_a_query_that_will_not_finish_is_interrupted(wide: Path) -> None:
    """The deliberately expensive query: a recursive CTE that never leaves its first step.

    It returns one row and would count to infinity to get there, so no cap can stop it and
    only the deadline can.
    """
    box = Sandbox(timeout_s=0.25)
    started = time.perf_counter()
    result = box.execute(wide, INFINITE_CTE)
    elapsed = time.perf_counter() - started

    assert not result.ok
    assert result.timed_out
    assert result.error == "statement timed out"
    assert 0.25 <= elapsed < 5.0


def test_the_deadline_is_checked_during_the_fetch_and_not_only_the_first_step(
    wide: Path,
) -> None:
    """A cross join returns its first row instantly and its 64,000,000th never.

    `conn.execute` therefore returns almost at once and every remaining second is spent in
    the fetch, so a deadline armed only around `execute` would pass this test's first
    assertion and hang for ever on the loop. The handler is set on the connection, which is
    what makes it cover both. The caps are lifted here on purpose — they would stop this
    query long before the deadline did, and that is the *next* test.
    """
    box = Sandbox(timeout_s=0.2, row_cap=10**9, byte_cap=10**12)
    started = time.perf_counter()
    result = box.execute(wide, "SELECT a.n FROM wide a, wide b, wide c")

    assert result.timed_out
    assert not result.truncated
    assert 0.2 <= time.perf_counter() - started < 5.0


def test_the_caps_stop_a_runaway_result_before_the_deadline_does(wide: Path) -> None:
    """The two controls answer different questions, and this is where that shows.

    Both queries here are the same 64,000,000-row cross join under the **real** limits, and
    neither reaches the 30-second deadline: a query that returns for ever is the caps'
    problem, and a query that computes for ever without returning is the deadline's.

    Which cap binds is decided by the row width, and the arithmetic is worth having written
    down. Three integer columns is 24 bytes a row, so 1 MiB runs out at 43,690 rows —
    before 50,000 rows do. One column is 8 bytes a row, so 50,000 rows is 400,000 bytes and
    the row cap binds first. Neither cap is redundant.
    """
    started = time.perf_counter()
    wide_rows = Sandbox().execute(wide, RUNAWAY_CROSS_JOIN)

    assert wide_rows.truncated_by == TRUNCATED_BY_BYTES
    assert wide_rows.result_bytes <= BYTE_CAP
    assert len(wide_rows.rows) == BYTE_CAP // 24
    assert not wide_rows.timed_out

    narrow = Sandbox().execute(wide, "SELECT a.n FROM wide a, wide b, wide c")

    assert narrow.truncated_by == TRUNCATED_BY_ROWS
    assert len(narrow.rows) == ROW_CAP
    assert not narrow.timed_out
    assert time.perf_counter() - started < STATEMENT_TIMEOUT_S


def test_the_timeout_is_named_rather_than_string_matched(wide: Path) -> None:
    """`timed_out` comes from the handler firing, not from reading SQLite's message.

    A user cancelling a query and a deadline expiring both raise `OperationalError:
    interrupted`, so 2.5 cannot tell them apart from the text. The flag can.
    """
    result = Sandbox(timeout_s=0.1).execute(wide, INFINITE_CTE)
    assert result.timed_out

    ordinary = Sandbox().execute(wide, "SELECT nope FROM wide")
    assert not ordinary.timed_out
    assert ordinary.error == "no such column: nope"


def test_a_quick_query_is_not_interrupted(singer: Path) -> None:
    result = Sandbox(timeout_s=0.25).execute(singer, "SELECT count(*) FROM singer")

    assert result.ok
    assert result.rows == ((3,),)
    assert not result.timed_out


# --- the caps --------------------------------------------------------------------------------


def test_a_large_result_is_capped_by_rows(wide: Path) -> None:
    result = Sandbox(row_cap=100).execute(wide, "SELECT n FROM wide")

    assert result.ok
    assert result.truncated
    assert result.truncated_by == TRUNCATED_BY_ROWS
    assert len(result.rows) == 100


def test_a_large_result_is_capped_by_bytes(wide: Path) -> None:
    """The other pathology: few rows, very large values.

    Each row here is an 8-byte integer plus 100 bytes of text, so a 1,080-byte cap admits
    exactly ten rows and the eleventh is what trips it.
    """
    result = Sandbox(byte_cap=10 * 108).execute(wide, "SELECT n, blob FROM wide")

    assert result.truncated_by == TRUNCATED_BY_BYTES
    assert len(result.rows) == 10
    assert result.result_bytes == 1080


def test_a_cap_is_checked_before_a_row_is_kept(wide: Path) -> None:
    """`truncated` means a row was dropped, and `result_bytes` never exceeds the cap.

    The alternative — appending and then noticing — would mark a result truncated when
    nothing was lost, and 2.4 counts truncated tasks as an argument for raising the cap.
    A miscount there argues for raising a cap that was never actually reached.
    """
    exact = Sandbox(byte_cap=400 * 108).execute(wide, "SELECT n, blob FROM wide")

    assert not exact.truncated
    assert len(exact.rows) == 400
    assert exact.result_bytes == 400 * 108 <= BYTE_CAP


def test_a_single_row_larger_than_the_byte_cap_returns_nothing_and_says_so(wide: Path) -> None:
    result = Sandbox(byte_cap=4).execute(wide, "SELECT n, blob FROM wide")

    assert result.rows == ()
    assert result.truncated_by == TRUNCATED_BY_BYTES
    assert result.result_bytes == 0


def test_a_result_under_both_caps_is_not_truncated(singer: Path) -> None:
    result = Sandbox().execute(singer, "SELECT name FROM singer")

    assert not result.truncated
    assert result.truncated_by is None


def test_truncation_reaches_the_equivalence_rule_as_its_own_reason(wide: Path) -> None:
    """The seam, asserted across the two modules rather than inside either.

    A capped candidate is **not** a wrong answer. Folding it into `row_count` would
    attribute a cap's effect to the model; `truncated` lets 2.4 count how many tasks a cap
    decided, and a non-zero count is an argument for raising the cap.
    """
    reference = [(n,) for n in range(400)]
    capped = Sandbox(row_cap=100).execute(wide, "SELECT n FROM wide")

    verdict = compare(reference, capped.rows, ordered=False, candidate_truncated=capped.truncated)

    assert verdict.reason == TRUNCATED
    assert not verdict.solved


# --- what a byte is --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "size"),
    [
        (None, 0),
        (1, 8),
        (2**62, 8),
        (1.5, 8),
        ("", 0),
        ("abc", 3),
        ("é", 2),
        ("日本語", 9),
        (b"\x00\x01\x02", 3),
        (bytearray(b"abcd"), 4),
    ],
)
def test_encoded_size_measures_the_value_not_the_object(value: object, size: int) -> None:
    """A large integer costs 8 the way SQLite stores it, and a multi-byte character costs
    its bytes. `sys.getsizeof(2**62)` is 32 on CPython 3.12 and would make the cap an
    interpreter detail."""
    assert encoded_size(value) == size


def test_result_size_is_what_the_cap_counts(wide: Path) -> None:
    result = Sandbox().execute(wide, "SELECT n, blob FROM wide")
    assert result.result_bytes == result_size(result.rows)


# --- errors are outcomes ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("SELEC 1", "syntax error"),
        ("SELECT nope FROM singer", "no such column"),
        ("SELECT * FROM nowhere", "no such table"),
        ("SELECT 1; SELECT 2", "one statement at a time"),
    ],
)
def test_sql_that_does_not_run_is_an_error_not_an_exception(
    singer: Path, sql: str, fragment: str
) -> None:
    result = Sandbox().execute(singer, sql)

    assert not result.ok
    assert fragment in result.error
    assert result.rows == ()


@pytest.mark.parametrize("sql", ["", "   \n\t ", "-- nothing at all", "/* just a comment */"])
def test_a_statement_with_no_statement_in_it_is_an_error(singer: Path, sql: str) -> None:
    """The trap this closes is specific and expensive.

    sqlite3 executes an empty string happily and returns zero rows. **49 of the frame's
    1,034 reference queries return no rows**, so a model that produced nothing parseable
    would silently solve every one of them — 4.7389% of the frame, for free, filed as a
    correct answer. It is an error here and named as one.
    """
    result = Sandbox().execute(singer, sql)

    assert not result.ok
    assert result.rows == ()
    assert "no statement" in result.error


def test_a_missing_database_is_an_error_not_an_exception(tmp_path: Path) -> None:
    result = Sandbox().execute(tmp_path / "absent.sqlite", "SELECT 1")

    assert not result.ok
    assert "unable to open database file" in result.error


def test_undecodable_bytes_come_back_replaced_rather_than_raising(tmp_path: Path) -> None:
    """The `wta_1` fault, in miniature.

    The tolerance belongs to the connection, not to the equivalence rule: both sides of
    every comparison are decoded the same way, so both carry U+FFFD and compare equal, and
    a decode fault is never read as a wrong answer.
    """
    path = tmp_path / "bytes.sqlite"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (name TEXT)")
        conn.execute("INSERT INTO t VALUES (CAST(x'6162ff63' AS TEXT))")
        conn.commit()
    finally:
        conn.close()

    result = Sandbox().execute(path, "SELECT name FROM t")

    assert result.ok
    assert result.rows == (("ab�c",),)


def test_a_limit_that_is_not_positive_is_refused() -> None:
    for kwargs in ({"timeout_s": 0}, {"row_cap": 0}, {"byte_cap": -1}, {"progress_interval": 0}):
        with pytest.raises(ValueError, match="positive"):
            Sandbox(**kwargs)


# --- the copy underneath ------------------------------------------------------------------------


def test_the_executor_never_opens_the_substrate(database_root: Path) -> None:
    with (
        SubstrateCopies(database_root) as copies,
        copies.for_task("singer", task_id="dev-0003") as path,
    ):
        assert path != database_path(database_root, "singer")
        assert database_root not in path.parents
        assert Sandbox().execute(path, "SELECT count(*) FROM singer").rows == ((3,),)


def test_run_scope_copies_each_database_once(database_root: Path) -> None:
    """Every connection is read-only, so a shared copy is the same isolation as a private
    one — at a fifth of the cost over the working set."""
    with SubstrateCopies(database_root, scope=CopyScope.RUN) as copies:
        with copies.for_task("singer", task_id="dev-0003") as first:
            pass
        with copies.for_task("singer", task_id="dev-0007") as second:
            pass
        with copies.for_task("wide", task_id="dev-0008") as other:
            pass

        assert first == second
        assert first.exists()
        assert other != first


def test_task_scope_gives_each_task_its_own_copy_and_removes_it(database_root: Path) -> None:
    """What Phase 4.2 needs: one attack case's mutation must not reach the next."""
    with SubstrateCopies(database_root, scope=CopyScope.TASK) as copies:
        with copies.for_task("singer", task_id="dev-0003") as first:
            assert first.exists()
        assert not first.exists()

        with copies.for_task("singer", task_id="dev-0007") as second:
            assert second.exists()
        assert first != second


def test_a_copy_outlives_damage_to_itself_without_touching_the_source(
    database_root: Path,
) -> None:
    """The whole point of the copy, made concrete: the sandbox refuses a write, but if
    something else ever did not, this is what absorbs it."""
    source = database_path(database_root, "singer")
    before = source.read_bytes()

    with (
        SubstrateCopies(database_root, scope=CopyScope.TASK) as copies,
        copies.for_task("singer", task_id="dev-0003") as path,
    ):
        conn = sqlite3.connect(path)
        try:
            conn.execute("DROP TABLE singer")
            conn.commit()
        finally:
            conn.close()
        assert not Sandbox().execute(path, "SELECT 1 FROM singer").ok

    assert source.read_bytes() == before


def test_closing_removes_every_copy(database_root: Path) -> None:
    copies = SubstrateCopies(database_root)
    with copies.for_task("singer", task_id="dev-0003"):
        pass
    root = copies.root
    assert root.exists()

    copies.close()
    assert not root.exists()
    copies.close()  # idempotent: a killed run's cleanup must not be a second fault


def test_an_unknown_database_is_refused_by_name(database_root: Path) -> None:
    with (
        SubstrateCopies(database_root) as copies,
        pytest.raises(FileNotFoundError, match="nowhere"),
        copies.for_task("nowhere", task_id="dev-0003"),
    ):
        pass


def test_a_write_ahead_log_is_copied_with_its_database(database_root: Path, tmp_path: Path) -> None:
    """No Spider database has one — checked over all 166 directories — but a database
    copied without its log silently loses whatever the log had not checkpointed, and
    finding that out from a wrong answer would be expensive."""
    source = database_path(database_root, "singer")
    source.with_name(source.name + "-wal").write_bytes(b"not a real log")

    with (
        SubstrateCopies(database_root) as copies,
        copies.for_task("singer", task_id="dev-0003") as path,
    ):
        assert path.with_name(path.name + "-wal").read_bytes() == b"not a real log"


# --- the caps are still above what the substrate returns -------------------------------------


def test_no_reference_result_in_the_frame_is_decided_by_a_cap() -> None:
    """The derivation, guarded in CI where there is no substrate.

    `docs/sandbox-caps.json` is committed by `scripts/sandbox_caps.py`. If a future change
    to the frame, the substrate or `encoded_size` pushes a legitimate result past a cap,
    this fails — which is the point. A cap that can decide a legitimate answer is a cap
    that silently moves the accuracy figure.
    """
    report = json.loads(CAPS_REPORT.read_text())

    assert report["frame_size"] == 1034
    assert report["rows"]["max"] == 20662
    assert report["bytes"]["max"] == 289104
    assert report["rows"]["max"] < ROW_CAP
    assert report["bytes"]["max"] < BYTE_CAP


def test_the_timeout_sits_far_above_the_slowest_reference_query() -> None:
    """Both halves of the derivation: above the slowest legitimate query, and below what
    would let a run of hung candidates be the thing that trips the wall-clock ceiling."""
    report = json.loads(CAPS_REPORT.read_text())
    slowest = report["seconds"]["max"]

    assert 100 * slowest < STATEMENT_TIMEOUT_S
    assert 150 * STATEMENT_TIMEOUT_S < 14400  # config/runs/working-set.toml


def test_the_caps_and_the_interval_are_the_values_the_derivations_argue_for() -> None:
    """A cap that drifts from the number its docstring derives is worse than no docstring."""
    assert (ROW_CAP, BYTE_CAP, STATEMENT_TIMEOUT_S, PROGRESS_INTERVAL) == (
        50_000,
        1_048_576,
        30.0,
        10_000,
    )
    assert sandbox.SIDECAR_SUFFIXES == ("-wal", "-shm", "-journal")
