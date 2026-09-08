"""The four tools: what they return, what they refuse, and what bounds them.

**No test here makes a live API call and none needs a key.** Every database is built by a
fixture; the one thing a stub cannot answer — whether a real model chooses sensibly among
four tools — is `scripts/a1_tool_probe.py`, which is a script and not a test for exactly
that reason.

Three things carry the weight. That **every error path returns rather than raises**, which
is 3.1's acceptance and the reason A1 can recover from a mistake at all. That
``describe_table`` returns **the same characters A0's prompt carries** for the same table,
which is constraint 41 checked rather than asserted. And that the rendering caps sit above
the largest legitimate result in the working set, read back from the artifact
`scripts/a1_tool_sizes.py` measured them into — so CI holds the derivation with no
substrate and no keys, the same shape as `docs/sandbox-caps.json`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from query_pilot.agents.schema import read_schema, render_schema
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
    describe_table,
    execute_sql,
    list_tables,
    render_rows,
    sample_rows,
)
from query_pilot.client.types import ToolCall
from query_pilot.sandbox import Sandbox, database_path

REPO = Path(__file__).resolve().parents[1]
TOOL_SIZES = REPO / "docs" / "a1-tool-sizes.json"


@pytest.fixture
def sandbox() -> Sandbox:
    return Sandbox()


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """A small database with the shapes the tools have to survive.

    A composite primary key, a foreign key with an implicit target column, an untyped
    column, a NULL, a BLOB, a value with a newline in it, and an empty table — each of
    which is a thing this substrate actually contains and each of which renders wrong if
    it is not handled.
    """
    path = database_path(tmp_path / "database", "concert")
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE stadium ("Stadium_ID" INT PRIMARY KEY, "Name" TEXT, "Capacity" INT);
            CREATE TABLE singer (id INT PRIMARY KEY, name TEXT NOT NULL, age INT, untyped);
            CREATE TABLE singer_in_concert (
                concert_id INT,
                singer_id INT,
                "Stadium_ID" INT,
                PRIMARY KEY (concert_id, singer_id),
                FOREIGN KEY (singer_id) REFERENCES singer(id),
                FOREIGN KEY ("Stadium_ID") REFERENCES stadium
            );
            CREATE TABLE empty_table (x INT);
            INSERT INTO stadium VALUES (1, 'Bercy', 41000), (2, 'Wembley', 90000);
            INSERT INTO singer VALUES (1, 'Joe', 30, NULL), (2, 'Rose
Marie', 41, x'DEADBEEF');
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path


# --- the schemas the model is offered ----------------------------------------------------


def test_there_are_exactly_four_tools_and_the_names_match_the_dispatch_table() -> None:
    assert TOOL_NAMES == ("list_tables", "describe_table", "sample_rows", "execute_sql")
    assert len(TOOL_SCHEMAS) == 4


@pytest.mark.parametrize("schema", TOOL_SCHEMAS, ids=lambda s: s.name)
def test_every_tool_schema_is_a_usable_json_schema_object(schema) -> None:
    """A parameter that is not described is a parameter that gets guessed.

    Both providers pass ``parameters`` through as a JSON Schema object — Groq inside
    ``{"type": "function", ...}`` and Google inside ``functionDeclarations`` — so a schema
    that is not an object with ``properties`` and ``required`` is a request one of them
    will refuse.
    """
    assert schema.description.strip()
    assert schema.parameters["type"] == "object"
    properties = schema.parameters["properties"]
    assert set(schema.parameters["required"]) <= set(properties)
    for name, spec in properties.items():
        assert spec["type"] in {"string", "integer"}, name
        assert spec["description"].strip(), name


def test_the_schemas_are_json_serialisable_as_the_providers_will_send_them() -> None:
    # Both adapters call dict(tool.parameters) and hand the result to json. A mapping that
    # is not serialisable would fail at the provider rather than here.
    json.dumps([dict(schema.parameters) for schema in TOOL_SCHEMAS])


# --- list_tables --------------------------------------------------------------------------


def test_list_tables_names_every_table_with_its_row_count(sandbox, database) -> None:
    result = list_tables(sandbox, database, {})
    assert result.ok
    assert "singer (2 rows)" in result.content
    assert "stadium (2 rows)" in result.content
    assert "empty_table (0 rows)" in result.content
    assert result.rows_returned == 4


def test_list_tables_ignores_sqlite_s_own_tables(sandbox, database, tmp_path) -> None:
    conn = sqlite3.connect(database)
    try:
        # An AUTOINCREMENT column is what makes SQLite create sqlite_sequence, and this
        # substrate has databases that have one. It is not part of anyone's question.
        conn.executescript(
            "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT); INSERT INTO t DEFAULT VALUES;"
        )
        conn.commit()
    finally:
        conn.close()
    assert "sqlite_" not in list_tables(sandbox, database, {}).content


def test_list_tables_takes_no_arguments_and_ignores_any_it_is_given(sandbox, database) -> None:
    # A model that sends an argument to a no-parameter tool has not made a mistake worth a
    # turn: the schema says there are none and the answer does not depend on them.
    assert list_tables(sandbox, database, {"table": "singer"}).ok


# --- describe_table -----------------------------------------------------------------------


def test_describe_table_returns_the_characters_a0_s_prompt_carries(sandbox, database) -> None:
    """Constraint 41, checked rather than asserted.

    A0 renders the whole schema into one prompt; A1 assembles the same description one
    table at a time. If these two ever diverge, 3.6 compares the descriptions as much as
    the agents — so the concatenation is required to be identical, not merely equivalent.
    """
    schema = read_schema(sandbox, database, "concert")
    described = [
        describe_table(sandbox, database, {"table": table.name}).content for table in schema.tables
    ]
    assert "\n\n".join(described) == render_schema(schema)


def test_describe_table_carries_keys_and_types_a_model_needs(sandbox, database) -> None:
    content = describe_table(sandbox, database, {"table": "singer_in_concert"}).content
    assert 'PRIMARY KEY ("concert_id", "singer_id")' in content
    assert 'FOREIGN KEY ("singer_id") REFERENCES "singer" ("id")' in content
    # SQLite leaves a foreign key's target column implicit when the reference names only a
    # table, and inventing one would tell the model something untrue.
    assert 'FOREIGN KEY ("Stadium_ID") REFERENCES "stadium"' in content


def test_describe_table_says_an_untyped_column_is_untyped(sandbox, database) -> None:
    assert '"untyped" UNTYPED' in describe_table(sandbox, database, {"table": "singer"}).content


# --- sample_rows ----------------------------------------------------------------------------


def test_sample_rows_shows_values_a_model_could_not_guess(sandbox, database) -> None:
    result = sample_rows(sandbox, database, {"table": "stadium"})
    assert result.ok
    assert "Bercy" in result.content
    assert "Stadium_ID | Name | Capacity" in result.content
    assert result.rows_returned == 2


def test_sample_rows_defaults_to_the_documented_default(sandbox, database) -> None:
    assert (
        f"Up to {SAMPLE_ROWS_DEFAULT} rows"
        in sample_rows(sandbox, database, {"table": "singer"}).content
    )


def test_sample_rows_clamps_an_over_large_limit_rather_than_refusing(sandbox, database) -> None:
    # A model asking for 500 rows wants to see the table, not to be corrected. Spending a
    # turn of a 150-task run on the correction buys nothing.
    result = sample_rows(sandbox, database, {"table": "singer", "limit": 500})
    assert result.ok
    assert f"Up to {SAMPLE_ROWS_MAX} rows" in result.content


def test_sample_rows_accepts_an_integer_written_as_a_string(sandbox, database) -> None:
    """Groq returns arguments as a JSON string and Google as an object.

    Both are normalised in `providers/`, but a model that writes ``"limit": "2"`` inside
    its own JSON produces a string on either path, and refusing it would spend a turn on a
    difference the model cannot see.
    """
    assert sample_rows(sandbox, database, {"table": "singer", "limit": "2"}).ok


def test_sample_rows_reports_an_empty_table_as_empty_rather_than_as_an_error(
    sandbox, database
) -> None:
    # An empty table is a fact about the database, and 49 of the frame's references return
    # no rows. Reading "no rows" as a failure is the same mistake `has_statement` exists to
    # stop one layer down.
    result = sample_rows(sandbox, database, {"table": "empty_table"})
    assert result.ok
    assert result.content.endswith("(no rows)")


# --- execute_sql -----------------------------------------------------------------------------


def test_execute_sql_returns_rows_through_the_same_sandbox(sandbox, database) -> None:
    result = execute_sql(sandbox, database, {"sql": "SELECT name FROM singer ORDER BY age"})
    assert result.ok
    assert result.rows_returned == 2
    assert "Joe" in result.content


def test_execute_sql_says_no_rows_distinctly_from_saying_error(sandbox, database) -> None:
    """3.4's recovery rate counts these apart, so the model must be able to tell them apart.

    "The query ran and matched nothing" asks for a different next move than "the query did
    not run", and a trajectory that confuses them repairs the wrong thing.
    """
    empty = execute_sql(sandbox, database, {"sql": "SELECT name FROM singer WHERE age > 999"})
    broken = execute_sql(sandbox, database, {"sql": "SELECT nmae FROM singer"})
    assert empty.ok and empty.content == "(no rows)"
    assert not broken.ok and broken.content.startswith("ERROR:")


def test_execute_sql_shows_at_most_the_row_cap_and_says_the_true_total(sandbox, database) -> None:
    rows = ", ".join(f"({n})" for n in range(RESULT_ROWS * 3))
    conn = sqlite3.connect(database)
    try:
        conn.executescript(f"CREATE TABLE many (n INT); INSERT INTO many VALUES {rows};")
        conn.commit()
    finally:
        conn.close()
    result = execute_sql(sandbox, database, {"sql": "SELECT n FROM many"})
    # Hiding the difference would let a capped result read as a complete one, which is the
    # failure the sandbox's own `truncated` flag exists to prevent one layer down.
    assert f"({RESULT_ROWS} of {RESULT_ROWS * 3} rows)" in result.content
    assert result.rows_returned == RESULT_ROWS * 3
    assert result.rows_shown == RESULT_ROWS


# --- error paths: 3.1's acceptance ------------------------------------------------------------
# Every one of these returns a structured error. None of them raises. That is what lets a
# model repair itself inside a trajectory instead of the task ending on its first mistake.


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        (describe_table, {}, "required"),
        (describe_table, {"table": 7}, "must be a string"),
        (describe_table, {"table": "  "}, "is empty"),
        (describe_table, {"table": "nope"}, "no table named"),
        (sample_rows, {}, "required"),
        (sample_rows, {"table": "nope"}, "no table named"),
        (sample_rows, {"table": "singer", "limit": "two"}, "must be an integer"),
        (sample_rows, {"table": "singer", "limit": 1.5}, "must be an integer"),
        (sample_rows, {"table": "singer", "limit": 0}, "at least 1"),
        (execute_sql, {}, "required"),
        (execute_sql, {"sql": 7}, "must be a string"),
        (execute_sql, {"sql": "SELECT nmae FROM singer"}, "no such column"),
        (execute_sql, {"sql": "SELECT * FROM nope"}, "no such table"),
        (execute_sql, {"sql": "-- just a comment"}, "no statement to execute"),
        (execute_sql, {"sql": "SELECT 1; DROP TABLE singer"}, "one statement"),
    ],
)
def test_an_error_path_returns_a_structured_error_rather_than_raising(
    sandbox, database, tool, arguments, expected
) -> None:
    result = tool(sandbox, database, arguments)
    assert isinstance(result, ToolResult)
    assert not result.ok
    assert result.error is not None
    assert expected in result.error
    # The model is shown the error, and shown that it is one.
    assert result.content == f"ERROR: {result.error}"


def test_an_unknown_table_is_answered_with_the_tables_that_do_exist(sandbox, database) -> None:
    # The cheapest mistake in a trajectory that has not called list_tables yet, and the
    # one most worth making recoverable in a single turn.
    error = describe_table(sandbox, database, {"table": "singers"}).error or ""
    assert "singer" in error and "stadium" in error


def test_an_unknown_tool_name_is_answered_rather_than_raised(sandbox, database) -> None:
    result = call_tool(sandbox, database, ToolCall(id="1", name="describe_tabel", arguments={}))
    assert not result.ok
    for name in TOOL_NAMES:
        assert name in (result.error or "")


def test_call_tool_dispatches_every_declared_tool(sandbox, database) -> None:
    arguments = {"table": "singer", "sql": "SELECT 1"}
    for name in TOOL_NAMES:
        result = call_tool(sandbox, database, ToolCall(id="1", name=name, arguments=arguments))
        assert result.ok, name


# --- rendering ------------------------------------------------------------------------------


def test_render_rows_spells_out_null_and_describes_a_blob(sandbox, database) -> None:
    # An empty cell and a NULL are different facts, and a model asked to write IS NULL has
    # to see which it is looking at. A BLOB's bytes are not something a model can use.
    content = sample_rows(sandbox, database, {"table": "singer"}).content
    assert "NULL" in content
    assert "<4 bytes>" in content


def test_render_rows_keeps_one_row_on_one_line(sandbox, database) -> None:
    # 'Rose\nMarie' is one value. A newline inside it would break the grid into two rows
    # and tell the model the table has more rows than it does.
    content = sample_rows(sandbox, database, {"table": "singer"}).content
    assert "Rose Marie" in content
    assert len([line for line in content.splitlines() if line.startswith("1 |")]) == 1


def test_render_rows_trims_a_value_at_the_value_cap() -> None:
    long_value = "x" * (VALUE_CHARS + 50)
    content = render_rows(["c"], [(long_value,)])
    assert f"{'x' * VALUE_CHARS}..." in content
    assert long_value not in content


def test_a_value_at_the_cap_exactly_is_not_trimmed() -> None:
    # A result that merely reaches a cap has lost nothing, the same distinction the
    # sandbox draws between reaching a cap and being truncated by one.
    exact = "x" * VALUE_CHARS
    assert f"| {exact} |" in render_rows(["a", "b", "c"], [("1", exact, "2")])


def test_the_backstop_bounds_a_result_no_other_cap_reached() -> None:
    # Ten rows of many wide columns: under the row cap and under the value cap, and still
    # far too large for a prompt the conversation has to carry to the end of a trajectory.
    columns = [f"c{n}" for n in range(60)]
    rows = [tuple("y" * VALUE_CHARS for _ in columns) for _ in range(RESULT_ROWS)]
    content = render_rows(columns, rows)
    assert len(content) > TOOL_RESULT_CHARS
    bounded = ToolResult(content=content)  # sanity: render_rows itself does not bound
    assert len(bounded.content) > TOOL_RESULT_CHARS


def test_every_tool_result_is_bounded_by_the_backstop(sandbox, tmp_path) -> None:
    path = database_path(tmp_path / "database", "wide")
    path.parent.mkdir(parents=True)
    columns = ", ".join(f'"c{n}" TEXT' for n in range(60))
    values = ", ".join(f"'{'y' * VALUE_CHARS}'" for _ in range(60))
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            f"CREATE TABLE wide ({columns});"
            + "".join(f"INSERT INTO wide VALUES ({values});" for _ in range(RESULT_ROWS))
        )
        conn.commit()
    finally:
        conn.close()
    for result in (
        sample_rows(sandbox, path, {"table": "wide", "limit": SAMPLE_ROWS_MAX}),
        execute_sql(sandbox, path, {"sql": "SELECT * FROM wide"}),
    ):
        assert len(result.content) <= TOOL_RESULT_CHARS + 64
        assert "trimmed to" in result.content


# --- the derivation, held by CI without a substrate --------------------------------------


def test_the_measured_derivation_is_committed_and_matches_the_caps_in_force() -> None:
    """`docs/a1-tool-sizes.json` is the artifact, and these are the caps it was taken at.

    A cap edited without re-running `scripts/a1_tool_sizes.py` fails here, which is the
    same guard `docs/sandbox-caps.json` puts on 2.2's caps: a number is allowed to be
    chosen, but it is not allowed to drift away from what it was chosen against.
    """
    sizes = json.loads(TOOL_SIZES.read_text())
    assert sizes["caps"] == {
        "RESULT_ROWS": RESULT_ROWS,
        "SAMPLE_ROWS_DEFAULT": SAMPLE_ROWS_DEFAULT,
        "SAMPLE_ROWS_MAX": SAMPLE_ROWS_MAX,
        "VALUE_CHARS": VALUE_CHARS,
    }
    assert sizes["tools"] == list(TOOL_NAMES)


def test_the_caps_sit_above_the_largest_legitimate_result_in_the_working_set() -> None:
    """The requirement 2.2 derived its own caps against, applied one layer up.

    A cap below the largest legitimate result does not bound a pathology — it silently
    decides what the model is shown, on the databases where it binds.
    """
    sizes = json.loads(TOOL_SIZES.read_text())
    assert sizes["longest_value"]["text_values_over_cap"] == 0
    assert sizes["longest_value"]["chars"] < VALUE_CHARS
    assert sizes["largest_tool_result"]["chars"] < TOOL_RESULT_CHARS


def test_an_exhaustive_trajectory_fits_inside_one_attempt_s_token_budget() -> None:
    """Constraint 46, which binds Phase 3 harder than it bound Phase 2.

    A1's last attempt carries every tool result of the trajectory. If the exhaustive case
    on the worst database does not fit under the strong endpoint's TPM at concurrency 1,
    the client's own bucket ends the run as `pools_exhausted` with no provider having
    refused anything — which is 2.4's near miss, one phase later and worse.
    """
    sizes = json.loads(TOOL_SIZES.read_text())
    budget = sizes["budget"]["budget_chars"]
    assert budget == round(
        (sizes["budget"]["tpm"] - sizes["budget"]["max_output_tokens"])
        * sizes["budget"]["chars_per_prompt_token"],
        1,
    )
    # Half the budget is the line: the other half is the system prompt, the question, the
    # assistant's own messages and the execute_sql results a real trajectory adds on top.
    assert sizes["worst_database"]["share_of_budget"] < 0.5
