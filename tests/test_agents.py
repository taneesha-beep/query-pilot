"""A0: what it puts in the prompt, what it gets out of a response, and what it records.

**No test here makes a live API call and none needs a key.** The client is a stub that
returns a scripted response, the databases are built by the fixtures, and the one test that
drives a real `Run` uses the same stub. The live acceptance run is `scripts/a0_smoke.py`,
which is not a test for exactly that reason.

The extraction tests carry the most weight. A baseline that fails to find SQL a model
correctly wrote reports a number that is too low, and every phase after this one is measured
against that number.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from query_pilot.agents import (
    A0,
    MAX_OUTPUT_TOKENS,
    ROLE,
    SYSTEM_PROMPT,
    build_prompt,
    extract_sql,
    project,
    read_schema,
    render_schema,
    results_name,
    split_statements,
    write_results,
)
from query_pilot.client.errors import ConfigError, ProviderHTTPError
from query_pilot.client.types import Completion
from query_pilot.equivalence import NO_SQL
from query_pilot.run import Run, RunConfig, RunLedger, TaskResult, read_rows
from query_pilot.sandbox import Sandbox, SubstrateCopies, database_path
from query_pilot.tasks import Task, load_split, load_tasks

REPO = Path(__file__).resolve().parents[1]
SPIDER = REPO / "data" / "spider"

QUESTION = "What are the names of every singer, oldest first?"
REFERENCE = "SELECT name FROM singer ORDER BY age DESC"
ANSWER = "SELECT name FROM singer ORDER BY age DESC"


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def database_root(tmp_path: Path) -> Path:
    root = tmp_path / "database"
    path = database_path(root, "concert")
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
            INSERT INTO singer VALUES (1, 'Joe', 30, NULL), (2, 'Rose', 41, NULL);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return root


@pytest.fixture
def task() -> Task:
    return Task("dev-0003", "concert", QUESTION, REFERENCE)


class StubClient:
    """Returns scripted responses. Records what it was asked and with what."""

    def __init__(self, *responses: str | Exception, finish_reason: str | None = "stop") -> None:
        self.responses = list(responses)
        self.finish_reason = finish_reason
        self.calls: list[tuple[str, list, int]] = []

    async def complete(self, role, messages, tools=None, *, max_output_tokens=512, **kwargs):
        self.calls.append((role, list(messages), max_output_tokens))
        answer = self.responses.pop(0) if self.responses else ""
        if isinstance(answer, Exception):
            raise answer
        return Completion(
            text=answer,
            tool_calls=(),
            prompt_tokens=400,
            completion_tokens=20,
            provider="groq",
            model="openai/gpt-oss-120b",
            pool="GROQ_API_KEY",
            latency_s=0.6,
            finish_reason=self.finish_reason,
        )


def agent(client, task: Task, database_root: Path, **kwargs) -> A0:
    return A0(client, [task], database_root, **kwargs)


# --- tasks ---------------------------------------------------------------------------------------


def test_a_split_is_read_in_the_order_it_lists() -> None:
    """The order is inside a run's fingerprint, so re-sorting would refuse every resume."""
    ids = load_split(REPO / "splits" / "smoke.json")

    assert len(ids) == 15
    assert ids[0] == "dev-0003"
    assert list(ids) == json.loads((REPO / "splits" / "smoke.json").read_text())["task_ids"]


def test_both_committed_split_shapes_are_read(tmp_path: Path) -> None:
    """`frame.json` says `tasks`; the three splits beside it say `task_ids`."""
    (tmp_path / "a.json").write_text('{"task_ids": ["dev-0001"]}')
    (tmp_path / "b.json").write_text('{"tasks": ["dev-0002"]}')
    (tmp_path / "c.json").write_text('{"nothing": []}')

    assert load_split(tmp_path / "a.json") == ("dev-0001",)
    assert load_split(tmp_path / "b.json") == ("dev-0002",)
    with pytest.raises(ValueError, match="neither"):
        load_split(tmp_path / "c.json")


def test_a_split_naming_a_task_the_substrate_lacks_is_refused(tmp_path: Path) -> None:
    """Not a shorter list: a run that quietly measured 149 of 150 reports the wrong rate."""
    (tmp_path / "dev.json").write_text('[{"db_id": "d", "question": "q", "query": "SELECT 1"}]')
    (tmp_path / "split.json").write_text('{"task_ids": ["dev-0000", "dev-0099"]}')

    with pytest.raises(KeyError, match="dev-0099"):
        load_tasks(tmp_path, tmp_path / "split.json")


@pytest.mark.skipif(not (SPIDER / "dev.json").exists(), reason="substrate not acquired")
def test_the_smoke_set_loads_from_the_real_substrate() -> None:
    tasks = load_tasks(SPIDER, REPO / "splits" / "smoke.json")

    assert len(tasks) == 15
    assert all(task.question and task.reference_sql and task.db_id for task in tasks)
    assert tasks[0].task_id == "dev-0003"


# --- the schema, read live ------------------------------------------------------------------------


def test_the_schema_comes_from_the_database_and_carries_all_four_things(
    database_root: Path,
) -> None:
    """Tables, columns, types and foreign keys — what 2.3 says the prompt must contain."""
    schema = read_schema(Sandbox(), database_path(database_root, "concert"), "concert")

    assert [table.name for table in schema.tables] == [
        "singer",
        "singer_in_concert",
        "stadium",
    ]
    singer = schema.tables[0]
    assert [(c.name, c.type) for c in singer.columns] == [
        ("id", "INT"),
        ("name", "TEXT"),
        ("age", "INT"),
        # Untyped columns are legal in SQLite and this substrate has them. Said plainly
        # rather than given an invented type the model would then trust.
        ("untyped", "UNTYPED"),
    ]
    assert singer.columns[0].primary_key == 1
    assert singer.columns[1].not_null
    # SQLite returns foreign keys in reverse declaration order; the set is the claim.
    assert {key.to_table for key in schema.tables[1].foreign_keys} == {"singer", "stadium"}


def test_a_composite_primary_key_is_rendered_as_one_constraint(database_root: Path) -> None:
    """`PRAGMA table_info` returns a **position**, not a bool.

    Reading it as a bool renders two inline `PRIMARY KEY` clauses, which is invalid DDL and
    tells the model the table has two separate keys. `singer_in_concert` in the real
    substrate has exactly this shape.
    """
    schema = read_schema(Sandbox(), database_path(database_root, "concert"), "concert")
    rendered = render_schema(schema)

    assert '  PRIMARY KEY ("concert_id", "singer_id")' in rendered
    assert '"concert_id" INT PRIMARY KEY' not in rendered


def test_a_foreign_key_with_no_named_column_renders_without_one(database_root: Path) -> None:
    """SQLite allows `REFERENCES stadium` with the column left implicit, and returns NULL
    for it. Rendering `REFERENCES "stadium" (None)` would be worse than saying less."""
    schema = read_schema(Sandbox(), database_path(database_root, "concert"), "concert")
    rendered = render_schema(schema)

    assert 'REFERENCES "stadium"' in rendered
    assert "None" not in rendered


def test_the_rendered_schema_is_valid_sqlite(database_root: Path) -> None:
    """The strongest check available without a model: SQLite itself accepts it back.

    A prompt whose DDL does not parse is one a model has to guess its way around, and this
    catches quoting, composite keys and implicit foreign keys in one assertion.
    """
    schema = read_schema(Sandbox(), database_path(database_root, "concert"), "concert")

    probe = sqlite3.connect(":memory:")
    try:
        probe.executescript(render_schema(schema))
    finally:
        probe.close()


def test_sqlite_s_own_tables_are_not_in_the_schema(database_root: Path) -> None:
    path = database_path(database_root, "concert")
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (a INTEGER PRIMARY KEY AUTOINCREMENT)")
        conn.commit()
    finally:
        conn.close()

    schema = read_schema(Sandbox(), path, "concert")
    assert not any(table.name.startswith("sqlite_") for table in schema.tables)


def test_an_unreadable_database_raises_rather_than_returning_an_empty_schema(
    tmp_path: Path,
) -> None:
    """The one place an exception is the honest outcome: this is our code reading our own
    substrate, and 1.3 reserves `executor_error` for exactly that."""
    with pytest.raises(RuntimeError, match="could not read the schema"):
        read_schema(Sandbox(), tmp_path / "absent.sqlite", "absent")


def test_an_identifier_holding_a_quote_survives_rendering(tmp_path: Path) -> None:
    """Spider's names are tame; Phase 4.2's planted ones will not be."""
    path = database_path(tmp_path, "odd")
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute('CREATE TABLE "we ""quote"" this" (a INT)')
        conn.commit()
    finally:
        conn.close()

    rendered = render_schema(read_schema(Sandbox(), path, "odd"))

    probe = sqlite3.connect(":memory:")
    try:
        probe.executescript(rendered)
    finally:
        probe.close()


# --- getting SQL out of a response -------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        "SELECT name FROM singer",
        "```sql\nSELECT name FROM singer\n```",
        "```\nSELECT name FROM singer\n```",
        "```sqlite\nSELECT name FROM singer\n```",
        "Here is the query: SELECT name FROM singer",
        "SELECT name FROM singer;",
        "```sql\nSELECT name FROM singer;\n```",
        "Sure!\n\n```sql\nSELECT name FROM singer\n```\n\nThis lists every singer.",
    ],
)
def test_the_four_shapes_a_model_answers_in_all_yield_the_statement(response: str) -> None:
    """Bare, fenced with a tag, fenced without one, and buried in prose."""
    assert extract_sql(response).sql == "SELECT name FROM singer"


def test_the_last_fenced_block_wins() -> None:
    """A model that shows its working and then corrects itself puts the answer last."""
    response = (
        "First attempt:\n```sql\nSELECT nmae FROM singer\n```\n"
        "That column is misspelled. Corrected:\n```sql\nSELECT name FROM singer\n```"
    )
    assert extract_sql(response).sql == "SELECT name FROM singer"


def test_a_fence_holding_no_statement_does_not_shadow_one_that_does() -> None:
    response = "```sql\nSELECT name FROM singer\n```\n```\n(no output)\n```"
    assert extract_sql(response).sql == "SELECT name FROM singer"


def test_a_with_clause_is_a_statement() -> None:
    response = "WITH old AS (SELECT * FROM singer WHERE age > 40) SELECT name FROM old"
    assert extract_sql(response).sql == response


def test_a_second_statement_is_dropped_and_counted() -> None:
    """2.3 owes a single statement. *Rejecting* multi-statement input is 4.1's control and
    a different layer, so this records rather than refuses."""
    extraction = extract_sql("```sql\nSELECT name FROM singer;\nDROP TABLE singer;\n```")

    assert extraction.sql == "SELECT name FROM singer"
    assert extraction.dropped == 1


def test_a_semicolon_inside_a_literal_does_not_end_the_statement() -> None:
    sql = "SELECT name FROM singer WHERE note = 'a;b' AND age > 1"
    assert extract_sql(sql).sql == sql
    assert split_statements(sql) == [sql]


def test_a_comment_cannot_hide_or_create_a_statement() -> None:
    assert extract_sql("-- SELECT name FROM singer").sql is None
    assert extract_sql("/* SELECT 1 */ SELECT name FROM singer").sql == "SELECT name FROM singer"
    assert extract_sql("SELECT '-- not a comment' AS a").sql == "SELECT '-- not a comment' AS a"


@pytest.mark.parametrize(
    "response",
    [
        "",
        "   ",
        "I cannot answer that.",
        # `\bWITH\b` alone matched the English word here and turned a refusal into a
        # syntax error. A CTE has a name and an `AS (`; a sentence does not.
        "I'm sorry, I can't help with that.",
        "That question cannot be answered with the tables available.",
        "```\n\n```",
        "-- nothing here",
        "```python\nx = 1\n```",
    ],
)
def test_a_response_with_no_statement_yields_none_with_a_reason(response: str) -> None:
    extraction = extract_sql(response)

    assert extraction.sql is None
    assert extraction.reason == "the response contained no SQL statement"


# --- the prompt ----------------------------------------------------------------------------


def test_the_prompt_holds_the_whole_schema_and_the_question(database_root: Path) -> None:
    schema = read_schema(Sandbox(), database_path(database_root, "concert"), "concert")

    messages = build_prompt(schema, QUESTION)

    assert [message.role for message in messages] == ["system", "user"]
    assert messages[0].content == SYSTEM_PROMPT
    user = messages[1].content
    assert "concert" in user
    for table in schema.tables:
        assert table.name in user
    # The varying half sits last, where an instruction is followed most reliably.
    assert user.index(QUESTION) > user.index("CREATE TABLE")
    assert user.rstrip().endswith("SQLite query:")


def test_the_prompt_holds_no_row_values(database_root: Path) -> None:
    """Sample rows would help A0 and belong to 3.1's tool layer. They would also give
    Phase 4.2's planted row values a second, unmeasured way into the prompt."""
    schema = read_schema(Sandbox(), database_path(database_root, "concert"), "concert")

    user = build_prompt(schema, QUESTION)[1].content

    assert "Joe" not in user and "Rose" not in user


def test_a0_asks_for_the_strong_role_and_never_names_a_model(
    task: Task, database_root: Path
) -> None:
    """Held constant against A1, which 5.2 fixes as the always-strong arm. A0 on the cheap
    model would report the loop's gain and the model's gain added together."""
    assert ROLE == "strong"
    client = StubClient(ANSWER)

    a0 = agent(client, task, database_root)
    try:
        _solve(a0, task)
    finally:
        a0.close()

    role, _, ceiling = client.calls[0]
    assert role == "strong"
    assert ceiling == MAX_OUTPUT_TOKENS
    # No call site names a model: the pinned strings live in config/providers.toml.
    assert "gpt-oss" not in SYSTEM_PROMPT


# --- one task, end to end -----------------------------------------------------------------------


class FakeContext:
    """The two things A0 uses off a `TaskContext`. The real one is exercised below."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.recorded: list[Completion] = []

    def record(self, completion: Completion, *, turn: int = 1) -> None:
        self.recorded.append(completion)


def _solve(a0: A0, task: Task):
    import asyncio

    context = FakeContext(task.task_id)
    return asyncio.run(a0.solve(context)), context


def test_a_correct_answer_is_solved_and_records_what_2_5_will_need(
    task: Task, database_root: Path
) -> None:
    a0 = agent(StubClient(f"```sql\n{ANSWER}\n```"), task, database_root)
    try:
        attempt, context = _solve(a0, task)
    finally:
        a0.close()

    assert attempt.comparison.solved
    assert attempt.sql == ANSWER
    assert len(context.recorded) == 1

    detail = attempt.detail()
    assert detail["solved"] is True and detail["reason"] == "solved"
    assert detail["db_id"] == "concert"
    assert detail["sql"] == ANSWER and detail["reference_sql"] == REFERENCE
    assert detail["provider"] == "groq" and detail["model"] == "openai/gpt-oss-120b"
    assert detail["prompt_tokens"] == 400 and detail["completion_tokens"] == 20
    assert detail["finish_reason"] == "stop"
    assert detail["candidate_rows"] == 2 and detail["reference_rows"] == 2
    assert detail["fenced"] is True and detail["dropped_statements"] == 0


def test_a_wrong_answer_is_a_non_solve_with_the_reason_kept(
    task: Task, database_root: Path
) -> None:
    a0 = agent(StubClient("SELECT name FROM singer ORDER BY age ASC"), task, database_root)
    try:
        attempt, _ = _solve(a0, task)
    finally:
        a0.close()

    assert not attempt.comparison.solved
    assert attempt.comparison.reason == "value_mismatch"


def test_sql_that_does_not_run_is_a_candidate_error_and_not_a_crash(
    task: Task, database_root: Path
) -> None:
    a0 = agent(StubClient("SELECT nmae FROM singer"), task, database_root)
    try:
        attempt, _ = _solve(a0, task)
    finally:
        a0.close()

    assert attempt.comparison.reason == "candidate_error"
    assert "no such column" in attempt.comparison.detail


def test_a_response_with_no_sql_is_its_own_reason(task: Task, database_root: Path) -> None:
    """Not `candidate_error`: "the SQL raised" and "there was no SQL" are different
    failures and 2.5 reads thirty of them by hand. Not `executor_error` either — that is
    1.3's label for this project's own bugs. And not a failed task, because a model that
    answered has already been paid for."""
    a0 = agent(StubClient("I'm sorry, I can't help with that."), task, database_root)
    try:
        attempt, context = _solve(a0, task)
    finally:
        a0.close()

    assert attempt.comparison.reason == NO_SQL
    assert not attempt.comparison.solved
    assert attempt.sql is None
    assert attempt.detail()["reason"] == "no_sql"
    # The attempt is still recorded: the tokens were spent whatever came back.
    assert len(context.recorded) == 1


def test_an_empty_response_does_not_solve_a_task_with_an_empty_reference(
    database_root: Path,
) -> None:
    """The trap the sandbox's `has_statement` closes, asserted from the agent's side.

    49 of the frame's 1,034 references return no rows and empty-against-empty is a solve,
    so a model that answered with nothing would score 4.7389% of the frame for free if this
    path ever came back solved.
    """
    empty = Task("dev-0003", "concert", QUESTION, "SELECT name FROM singer WHERE 1 = 0")
    a0 = agent(StubClient(""), empty, database_root)
    try:
        attempt, _ = _solve(a0, empty)
    finally:
        a0.close()

    assert not attempt.comparison.solved
    assert attempt.comparison.reason == NO_SQL


def test_a_provider_failure_propagates_rather_than_scoring_as_a_non_solve(
    task: Task, database_root: Path
) -> None:
    """A quota wall must never be able to move the accuracy figure. The run loop records
    the task **failed** and a resume retries it; only answers we actually received are
    scored."""
    refusal = ProviderHTTPError(
        status=429, body="rate limited", provider="groq", model="m", pool="GROQ_API_KEY"
    )
    a0 = agent(StubClient(refusal), task, database_root)
    try:
        with pytest.raises(ProviderHTTPError):
            _solve(a0, task)
    finally:
        a0.close()


def test_the_schema_is_read_once_per_database_not_once_per_task(
    database_root: Path,
) -> None:
    tasks = [
        Task("dev-0003", "concert", QUESTION, REFERENCE),
        Task("dev-0004", "concert", QUESTION, REFERENCE),
    ]
    counting = _CountingSandbox()
    a0 = A0(StubClient(ANSWER, ANSWER), tasks, database_root, sandbox=counting)
    try:
        for one in tasks:
            _solve(a0, one)
    finally:
        a0.close()

    assert counting.schema_reads == 1


class _CountingSandbox(Sandbox):
    def __init__(self) -> None:
        super().__init__()
        self.schema_reads = 0

    def execute(self, database, sql):
        if "sqlite_master" in sql:
            self.schema_reads += 1
        return super().execute(database, sql)


def test_one_copy_is_made_per_database_and_removed_at_the_end(
    task: Task, database_root: Path
) -> None:
    copies = SubstrateCopies(database_root)
    a0 = A0(StubClient(ANSWER), [task], database_root, copies=copies)
    _solve(a0, task)
    assert copies.root.exists()

    a0.close()
    assert not copies.root.exists()
    assert database_path(database_root, "concert").exists()


def test_the_committed_smoke_run_declares_ceilings_that_trace_to_measured_capacity() -> None:
    """2.3's acceptance run is held to a declaration like any other. The ceilings are the
    one place this project writes an unmeasured number, so the arithmetic is tested."""
    config = RunConfig.load(
        REPO / "config" / "runs" / "smoke-set.toml",
        [f"dev-{index:04d}" for index in range(10)],
        run_id="r-smoke",
    )

    # The same 10,000-tokens-a-task row of docs/PROVIDERS.md that working-set.toml uses,
    # scaled to ten tasks. A hypothetical, and 2.4 is what replaces it with a measurement.
    assert config.token_ceiling == 10 * 10_000 == 100_000
    # Ten tasks at Groq's observed 30 RPM is a floor of 20 seconds of request time.
    assert config.wall_clock_ceiling_s == 600 > 10 / 30 * 60
    assert config.agent == "A0"
    assert config.params == {"split": "smoke", "limit": 10}


# --- through a real run --------------------------------------------------------------------------


async def test_a0_is_a_run_executor_and_its_verdict_reaches_the_ledger(
    task: Task, database_root: Path, tmp_path: Path
) -> None:
    """The seam 1.3 built, closed by 2.3: `run/` writes the comparison into a task row's
    free-form `detail` without ever looking inside it, and never learns what a solve is."""
    ledger = RunLedger("r-a0", root=tmp_path / "runs")
    config = RunConfig(
        run_id="r-a0",
        agent="A0",
        task_ids=(task.task_id,),
        token_ceiling=10_000,
        wall_clock_ceiling_s=60.0,
        concurrency=1,
    )
    a0 = A0(StubClient(f"```sql\n{ANSWER}\n```"), [task], database_root)

    try:
        with ledger:
            report = await Run(config, ledger).execute(a0)
    finally:
        a0.close()

    assert report.complete
    assert report.tasks_complete == 1
    assert report.prompt_tokens == 400 and report.completion_tokens == 20

    rows = [row for row in read_rows(ledger.path) if row["kind"] == "task"]
    assert len(rows) == 1
    assert rows[0]["detail"]["solved"] is True
    assert rows[0]["detail"]["sql"] == ANSWER


# --- the projection: a run's ledger, folded into the file that gets committed -------------------


def _ledger_with(tmp_path: Path, outcomes, *, run_id="r-1", declared=None):
    """Drive a real Run with stub task outcomes, so the projection reads a real ledger."""
    ids = list(declared or [t for t, _ in outcomes])
    config = RunConfig.start(
        "A0", ids, run_id=run_id, token_ceiling=10_000_000, wall_clock_ceiling_s=3600.0
    )
    ledger = RunLedger(run_id, root=tmp_path / "runs")
    detail_of = dict(outcomes)

    async def executor(context):
        context.record(
            Completion(
                text="x",
                tool_calls=(),
                prompt_tokens=400,
                completion_tokens=100,
                provider="groq",
                model="openai/gpt-oss-120b",
                pool="groq#1",
                finish_reason="stop",
                latency_s=0.5,
            )
        )
        detail = detail_of[context.task_id]
        if isinstance(detail, BaseException):
            raise detail
        return TaskResult(detail=detail)

    return ledger, config, executor


def _solved(sql="SELECT 1", *, reference_rows=1):
    return {
        "solved": True,
        "reason": "solved",
        "db_id": "db",
        "sql": sql,
        "reference_sql": "SELECT 1",
        "reference_rows": reference_rows,
    }


def _unsolved(reason="row_count", **extra):
    return {
        "solved": False,
        "reason": reason,
        "db_id": "db",
        "sql": "SELECT 2",
        "reference_sql": "SELECT 1",
        "reference_rows": 1,
        **extra,
    }


async def test_the_projection_counts_what_the_ledger_says_and_names_where_it_came_from(tmp_path):
    ledger, config, executor = _ledger_with(
        tmp_path,
        [
            ("t-0", _solved()),
            ("t-1", _solved()),
            ("t-2", _unsolved()),
            ("t-3", _unsolved("no_sql")),
        ],
    )
    with ledger:
        await Run(config, ledger).execute(executor)

    document = project(ledger.path, split="working", empty_reference_tasks=1)

    assert document["execution_accuracy"] == {
        "solved": 2,
        "of": 4,
        "percent": 50.0,
        "empty_result_floor": {
            "tasks": 1,
            "of": 4,
            "percent": 25.0,
            "what_it_means": document["execution_accuracy"]["empty_result_floor"]["what_it_means"],
        },
    }
    assert document["reasons"] == {"solved": 2, "row_count": 1, "no_sql": 1}
    # Constraint 12: a figure without its provider, model, date and ledger is not a result.
    measurement = document["measurement"]
    assert measurement["provider_and_model"] == ["groq/openai/gpt-oss-120b"]
    assert measurement["ledger"] == str(ledger.path)
    assert measurement["run_id"] == "r-1" and measurement["date"][:2] == "20"
    assert document["tokens"]["total"] == 2000 and document["tokens"]["money"] == 0.0
    assert document["tokens"]["per_solved_task"] == 1000.0


async def test_a_run_that_did_not_answer_every_task_gets_no_rate(tmp_path):
    """The same refusal 1.3 makes: 2 of 3 answered is not a percentage of anything."""
    ledger, config, executor = _ledger_with(
        tmp_path,
        [
            ("t-0", _solved()),
            ("t-1", ConfigError("no credential")),
            ("t-2", _solved()),
        ],
    )
    with ledger:
        await Run(config, ledger).execute(executor)

    document = project(ledger.path, split="working", empty_reference_tasks=0)

    # A ConfigError ends the run at the first task, so t-2 was never asked.
    assert document["run"]["tasks_complete"] == 1 and document["run"]["tasks_failed"] == 1
    assert str(document["execution_accuracy"]["percent"]).startswith("TBD (run incomplete")
    assert str(document["tokens"]["per_solved_task"]).startswith("TBD")
    failed = [t for t in document["tasks"] if t["status"] == "failed"]
    assert failed[0]["exception"] == "ConfigError" and "solved" not in failed[0]
    # The message stays in the ledger and out of the committed file.
    assert "message" not in failed[0]


async def test_a_figure_the_projection_was_not_given_reads_tbd_rather_than_being_invented(tmp_path):
    ledger, config, executor = _ledger_with(tmp_path, [("t-0", _solved())])
    with ledger:
        await Run(config, ledger).execute(executor)

    document = project(ledger.path, split="working")

    assert document["execution_accuracy"]["empty_result_floor"]["tasks"] == "TBD"
    assert document["execution_accuracy"]["empty_result_floor"]["percent"] == "TBD"
    assert "by_difficulty" not in document


async def test_the_projection_carries_what_2_5_reads_thirty_of_by_hand(tmp_path):
    ledger, config, executor = _ledger_with(
        tmp_path,
        [
            (
                "t-0",
                _unsolved(
                    "value_mismatch",
                    detail="row 0 column 1: 41 != 42",
                    candidate_rows=3,
                    candidate_timed_out=False,
                ),
            )
        ],
    )
    with ledger:
        await Run(config, ledger).execute(executor)

    document = project(
        ledger.path, split="working", difficulty={"t-0": "hard"}, empty_reference_tasks=0
    )

    (row,) = document["tasks"]
    assert row["sql"] == "SELECT 2" and row["reference_sql"] == "SELECT 1"
    assert row["detail"] == "row 0 column 1: 41 != 42"
    assert row["difficulty"] == "hard"
    assert document["by_difficulty"] == {"hard": {"solved": 0, "of": 1, "percent": 0.0}}


# --- one projection, two agents ---------------------------------------------------------------


async def test_the_projection_carries_a_trajectory_agents_own_fields(tmp_path):
    """**The projection never learns which agent wrote a `detail`, and this is what that buys.**

    A1's rows carry a termination, turn and tool-call counts, 3.3's repair counters and the
    validation rule that rejected a reply. All of it rides in the same free-form `detail` A0
    uses, and the projection copies a key when the row has one rather than knowing whose it
    is. `validation_rule` matters most: `no_sql` means two different things for A1 -- no
    statement in the answer, or a reply the validator rejected -- and grouping those apart
    needs the rule, which is deliberately not a ninth equivalence slug.
    """
    ledger, config, executor = _ledger_with(
        tmp_path,
        [
            (
                "t-0",
                _solved()
                | {
                    "termination": "answer",
                    "turns": 4,
                    "tool_calls": 3,
                    "tool_calls_by_name": {"list_tables": 1, "execute_sql": 2},
                    "repair_attempts": 1,
                    "repair_succeeded": True,
                    "repair_blocked": None,
                    "validation_rule": None,
                    "transcript": "transcripts/t-0.jsonl",
                },
            ),
            (
                "t-1",
                _unsolved("no_sql")
                | {
                    "termination": "tool_call_limit",
                    "turns": 14,
                    "tool_calls": 12,
                    "repair_attempts": 0,
                    "repair_blocked": "termination",
                    "validation_rule": "no_statement",
                },
            ),
        ],
    )
    with ledger:
        await Run(config, ledger).execute(executor)

    document = project(ledger.path, split="working", empty_reference_tasks=0)
    first, second = document["tasks"]

    assert first["termination"] == "answer" and first["turns"] == 4
    assert first["tool_calls_by_name"] == {"list_tables": 1, "execute_sql": 2}
    assert first["repair_attempts"] == 1 and first["repair_succeeded"] is True
    assert first["transcript"] == "transcripts/t-0.jsonl"
    assert second["repair_blocked"] == "termination"
    assert second["validation_rule"] == "no_statement"

    # And the two aggregates only a trajectory agent can produce.
    assert document["terminations"] == {"answer": 1, "tool_call_limit": 1}
    assert document["validation_rules"] == {"no_statement": 1}


async def test_a_single_shot_runs_projection_is_unchanged_by_those_fields_existing(tmp_path):
    """**Constraint 48: nothing may re-score A0.** A0's rows carry none of A1's keys, so the
    keys that carry them must leave a single-shot projection byte for byte what it was --
    including not growing two empty mappings that would say nothing about it."""
    ledger, config, executor = _ledger_with(tmp_path, [("t-0", _solved()), ("t-1", _unsolved())])
    with ledger:
        await Run(config, ledger).execute(executor)

    document = project(ledger.path, split="working", empty_reference_tasks=0)

    assert "terminations" not in document and "validation_rules" not in document
    for row in document["tasks"]:
        assert not {"termination", "turns", "tool_calls", "validation_rule"} & set(row)


def test_a_projection_is_named_after_the_agent_and_the_split_it_declared():
    """The filename is derived, not a constant, because one projection serves both agents and
    a constant naming one of them would be the single line in this module that knew."""
    assert results_name({"measurement": {"agent": "A0", "split": "working"}}) == "a0-working.json"
    assert results_name({"measurement": {"agent": "A1", "split": "working"}}) == "a1-working.json"
    assert results_name({"measurement": {"agent": "A1", "split": "smoke"}}) == "a1-smoke.json"


@pytest.mark.parametrize(
    "measurement", [{}, {"agent": "A1"}, {"split": "working"}, {"agent": None, "split": "working"}]
)
def test_a_projection_missing_either_half_of_its_name_is_refused_rather_than_guessed(measurement):
    """A file called `none-none.json` is worse than a refusal."""
    with pytest.raises(ValueError, match="agent and the split"):
        results_name({"measurement": measurement})


async def test_write_results_names_the_file_itself_when_given_a_directory(tmp_path):
    ledger, config, executor = _ledger_with(tmp_path, [("t-0", _solved())])
    with ledger:
        await Run(config, ledger).execute(executor)

    document = project(ledger.path, split="working", empty_reference_tasks=0)
    written = write_results(document, tmp_path / "results")
    assert written.name == "a0-working.json"

    # An explicit filename still wins: the caller that names one means it.
    named = write_results(document, tmp_path / "results" / "elsewhere.json")
    assert named.name == "elsewhere.json"
