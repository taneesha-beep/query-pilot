"""A1's loop: what ends a trajectory, what that costs the task, and what lands on disk.

**No test here makes a live API call and none needs a key.** The client is a stub that
returns a scripted sequence of tool calls and answers. The one thing a stub cannot show —
whether a real model drives four tools sensibly — is `scripts/a1_tool_probe.py`, which spent
nine requests on 2026-09-08 and is recorded in `docs/a1-tool-probe.json`.

Three things carry the weight here. **The replay guarantee**: a transcript rebuilt from disk
must equal the messages the client was actually handed, because 3.4, 4.3 and 7.2 all read
that file and a reshape after 3.6 means re-running a measured run. **Each limit's two
edges**, admitting a trajectory that should finish and stopping one that should not, because
a limit no task can reach is not a limit and one a legitimate trajectory hits silently moves
the accuracy figure. And **which outcomes fail the task**, because 1.3's resume retries
failed tasks and skips complete ones, so that decision is what a resumed run re-runs.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from query_pilot.agents.a0 import ANSWER_RULES
from query_pilot.agents.a1 import (
    A1,
    ANSWER,
    BUDGET,
    PROMPT_CEILING,
    PROMPT_CEILING_CHARS,
    SYSTEM_PROMPT,
    TERMINATIONS,
    TOOL_CALL_LIMIT,
    TOOL_CALL_LIMIT_REACHED,
    TURN_LIMIT,
    TURN_LIMIT_REACHED,
    build_prompt,
    conversation_chars,
)
from query_pilot.agents.tools import TOOL_NAMES
from query_pilot.agents.transcript import (
    END,
    MESSAGE,
    START,
    TOOL_RESULT,
    read_trajectories,
    read_transcript,
    replay,
    transcript_path,
)
from query_pilot.client.errors import ProviderHTTPError
from query_pilot.client.types import Completion, Message, ToolCall
from query_pilot.equivalence import NO_SQL
from query_pilot.run import COMPLETE, FAILED, Run, RunConfig, RunLedger, read_rows
from query_pilot.sandbox import database_path
from query_pilot.tasks import Task

REPO = Path(__file__).resolve().parents[1]
TOOL_SIZES = REPO / "docs" / "a1-tool-sizes.json"
TOOL_PROBE = REPO / "docs" / "a1-tool-probe.json"

QUESTION = "What are the names of every singer, oldest first?"
REFERENCE = "SELECT name FROM singer ORDER BY age DESC"
ANSWER_SQL = "SELECT name FROM singer ORDER BY age DESC"


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
            CREATE TABLE singer (id INT PRIMARY KEY, name TEXT NOT NULL, age INT);
            INSERT INTO singer VALUES (1, 'Joe', 30), (2, 'Rose', 41);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return root


@pytest.fixture
def task() -> Task:
    return Task("dev-0003", "concert", QUESTION, REFERENCE)


def call(name: str, **arguments) -> ToolCall:
    return ToolCall(id=f"call-{name}", name=name, arguments=arguments)


class StubClient:
    """Returns a scripted sequence. Records the messages it was handed, per turn.

    ``handed`` is what makes the replay guarantee testable: the transcript is rebuilt from
    disk and compared against exactly these lists, rather than against a second idea of what
    should have been sent.
    """

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.handed: list[list[Message]] = []
        self.tools_offered: list[tuple[str, ...]] = []

    async def complete(self, role, messages, tools=None, *, max_output_tokens=512, **kwargs):
        self.handed.append([m for m in messages])
        self.tools_offered.append(tuple(t.name for t in tools or ()))
        answer = self.responses.pop(0) if self.responses else ""
        if isinstance(answer, Exception):
            raise answer
        text, calls = answer if isinstance(answer, tuple) else (answer, ())
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


async def run_one(client, task, database_root, tmp_path, **kwargs):
    """Drive one task through a real `Run`, so the ledger and the guard are the real ones."""
    run_id = "20260908-000000-aaaaaa"
    ledger = RunLedger(run_id, root=tmp_path / "runs")
    config = RunConfig.start(
        "A1",
        [task.task_id],
        token_ceiling=kwargs.pop("token_ceiling", 1_000_000),
        wall_clock_ceiling_s=kwargs.pop("wall_clock_ceiling_s", 3_600),
        run_id=run_id,
    )
    agent = A1(client, [task], database_root, ledger.directory, **kwargs)
    try:
        with ledger:
            report = await Run(config, ledger).execute(agent)
    finally:
        agent.close()
    rows = [r for r in read_rows(ledger.path) if r["kind"] == "task"]
    return report, rows[0], ledger.directory


# --- the prompt: what is held constant against A0 ---------------------------------------------


def test_a1_shares_a0_s_answer_rules_word_for_word() -> None:
    """The A0-against-A1 figure is about the loop only if everything else is held constant.

    A second copy of these five lines is a second thing that can drift, and a drift would be
    reported under the loop's name.
    """
    assert ANSWER_RULES in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.endswith(ANSWER_RULES)


def test_a0_s_system_prompt_is_still_the_string_it_was_measured_with() -> None:
    """124 of 150 was taken with this exact prompt, so the 3.2 refactor must not have moved it.

    Pinned as a literal rather than recomposed from the parts, because a test that rebuilt
    it the same way the module does would pass however both changed.
    """
    from query_pilot.agents.a0 import SYSTEM_PROMPT as A0_PROMPT

    assert A0_PROMPT == (
        "You are an expert SQLite analyst. You are given the complete schema of one SQLite "
        "database and one question about it. Reply with a single SQLite SELECT statement "
        "that answers the question, and nothing else.\n"
        "\n"
        "Rules:\n"
        "- Use only the tables and columns in the schema, spelled exactly as they appear.\n"
        "- Return exactly the columns the question asks for, in the order it asks for them, "
        "and no others.\n"
        "- Add ORDER BY only when the question asks for an order.\n"
        "- Add DISTINCT only when the question asks for distinct values.\n"
        "- Write one statement. Do not explain it, and do not end it with a semicolon."
    )


def test_the_opening_prompt_carries_no_schema() -> None:
    """Discovering it is the work, and that is the whole difference from A0."""
    prompt = build_prompt("concert", QUESTION)
    body = "\n".join(message.content for message in prompt)
    # The question names a singer; what must be absent is any description of the database.
    assert "CREATE TABLE" not in body
    assert "PRIMARY KEY" not in body
    assert "concert" in body and QUESTION in body


@pytest.mark.asyncio
async def test_all_four_tools_are_offered_on_every_turn(task, database_root, tmp_path) -> None:
    client = StubClient(("", [call("list_tables")]), ANSWER_SQL)
    await run_one(client, task, database_root, tmp_path)
    assert client.tools_offered == [TOOL_NAMES, TOOL_NAMES]


# --- termination: the four completing paths ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_with_no_tool_calls_is_the_answer(task, database_root, tmp_path) -> None:
    client = StubClient(
        ("", [call("list_tables")]),
        ("", [call("describe_table", table="singer")]),
        ANSWER_SQL,
    )
    _, row, _ = await run_one(client, task, database_root, tmp_path)
    assert row["status"] == COMPLETE
    assert row["detail"]["termination"] == ANSWER
    assert row["detail"]["solved"] is True
    assert row["detail"]["turns"] == 3
    assert row["detail"]["tool_calls"] == 2


@pytest.mark.asyncio
async def test_an_answer_with_no_sql_scores_no_sql_rather_than_looking_back(
    task, database_root, tmp_path
) -> None:
    """Scored the way A0's single response is, deliberately.

    Rescuing the trajectory by scoring the last `execute_sql` the model ran would put a
    thumb on a scale A0 has no equivalent of: A0 gets one response and lives with it.
    """
    client = StubClient(
        ("", [call("execute_sql", sql=ANSWER_SQL)]),
        "I could not work this out.",
    )
    _, row, _ = await run_one(client, task, database_root, tmp_path)
    assert row["status"] == COMPLETE
    assert row["detail"]["termination"] == ANSWER
    assert row["detail"]["reason"] == NO_SQL
    assert row["detail"]["sql"] is None


@pytest.mark.asyncio
async def test_the_turn_limit_stops_a_trajectory_and_the_task_is_complete(
    task, database_root, tmp_path
) -> None:
    """Complete, not failed, and that decides what a resumed run re-runs.

    The run got answers and paid for every turn; constraint 25 says complete means the run
    got an answer, whatever the answer was. Retrying would spend the whole trajectory again
    to hit the same wall.
    """
    client = StubClient(*[("", [call("list_tables")])] * 8)
    _, row, _ = await run_one(client, task, database_root, tmp_path, turn_limit=3)
    assert row["status"] == COMPLETE
    assert row["detail"]["termination"] == TURN_LIMIT_REACHED
    assert row["detail"]["turns"] == 3
    assert row["detail"]["reason"] == NO_SQL


@pytest.mark.asyncio
async def test_the_last_turn_s_tool_calls_are_not_executed(task, database_root, tmp_path) -> None:
    """There is no turn left to feed them back into, so running them is pure waste.

    The transcript keeps the assistant message with its unexecuted calls, which is what a
    trajectory that ended mid-reach actually looks like — and 3.4 must not count them as
    tool calls that happened.
    """
    client = StubClient(*[("", [call("list_tables")])] * 5)
    _, row, directory = await run_one(client, task, database_root, tmp_path, turn_limit=2)
    assert row["detail"]["turns"] == 2
    # Two turns asked for a tool; only the first turn's call had a turn left to use it.
    assert row["detail"]["tool_calls"] == 1
    events = read_transcript(transcript_path(directory, task.task_id))
    assert len([e for e in events if e["kind"] == TOOL_RESULT]) == 1
    assert len([e for e in events if e["kind"] == MESSAGE and e["tool_calls"]]) == 2


@pytest.mark.asyncio
async def test_the_tool_call_limit_stops_a_trajectory_and_the_task_is_complete(
    task, database_root, tmp_path
) -> None:
    client = StubClient(*[("", [call("list_tables")])] * 10)
    _, row, _ = await run_one(
        client, task, database_root, tmp_path, turn_limit=10, tool_call_limit=3
    )
    assert row["status"] == COMPLETE
    assert row["detail"]["termination"] == TOOL_CALL_LIMIT_REACHED
    assert row["detail"]["tool_calls"] == 3


@pytest.mark.asyncio
async def test_the_tool_call_limit_counts_across_turns_not_within_one(
    task, database_root, tmp_path
) -> None:
    """The two limits are independent, and this is the case that shows it.

    Three turns of two calls each is six calls inside a turn limit of ten. Only a limit that
    accumulates across turns catches it, and the format allows a message to carry several
    calls even though `openai/gpt-oss-120b` was measured emitting one.
    """
    both = ("", [call("list_tables"), call("describe_table", table="singer")])
    client = StubClient(both, both, both, both)
    _, row, _ = await run_one(
        client, task, database_root, tmp_path, turn_limit=10, tool_call_limit=5
    )
    assert row["detail"]["termination"] == TOOL_CALL_LIMIT_REACHED
    assert row["detail"]["tool_calls"] == 5
    assert row["detail"]["turns"] == 3


@pytest.mark.asyncio
async def test_the_prompt_ceiling_stops_a_trajectory_before_the_request_is_sent(
    task, database_root, tmp_path
) -> None:
    """The fifth termination path, and the one the roadmap does not name.

    Checked before the request rather than after, because the request is the thing that
    would push the per-minute bucket into the deficit that ends the whole run.
    """
    client = StubClient(*[("", [call("list_tables")])] * 6)
    ceiling = len(SYSTEM_PROMPT) + 200
    _, row, _ = await run_one(
        client, task, database_root, tmp_path, turn_limit=10, prompt_ceiling_chars=ceiling
    )
    assert row["status"] == COMPLETE
    assert row["detail"]["termination"] == PROMPT_CEILING
    # It sent at least one request: a ceiling that stopped the first would be a broken
    # declaration, not a bounded run, and A1 should degrade to one shot rather than to none.
    assert row["detail"]["turns"] >= 1
    assert len(client.handed) == row["detail"]["turns"]


def test_a_prompt_ceiling_that_admits_no_trajectory_is_refused(database_root, tmp_path) -> None:
    with pytest.raises(ValueError, match="prompt ceiling"):
        A1(StubClient(), [], database_root, tmp_path, prompt_ceiling_chars=len(SYSTEM_PROMPT))


# --- termination: the one that fails the task ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_budget_guard_fails_the_task_so_a_resume_retries_it(
    task, database_root, tmp_path
) -> None:
    """The run stopped; the task did not answer. A run-level stop must never put a non-solve
    into the accuracy figure, which is the same rule a quota wall follows in 2.3.
    """
    client = StubClient(*[("", [call("list_tables")])] * 6)
    report, row, _ = await run_one(
        client, task, database_root, tmp_path, token_ceiling=500, turn_limit=10
    )
    assert row["status"] == FAILED
    assert row["detail"]["exception"] == "BudgetStopped"
    assert report.incomplete_reason == "token_ceiling"
    # `solved` is absent rather than false: the model never got to be wrong.
    assert "solved" not in row["detail"]


@pytest.mark.asyncio
async def test_a_client_error_propagates_and_fails_the_task(task, database_root, tmp_path) -> None:
    """A quota wall may never move the accuracy figure. Same rule A0 follows, same reason."""
    refusal = ProviderHTTPError(
        status=429,
        body="rate limit",
        provider="groq",
        model="openai/gpt-oss-120b",
        pool="GROQ_API_KEY",
    )
    client = StubClient(("", [call("list_tables")]), refusal)
    _, row, _ = await run_one(client, task, database_root, tmp_path)
    assert row["status"] == FAILED
    assert "solved" not in row["detail"]


@pytest.mark.asyncio
async def test_a_tool_error_goes_back_to_the_model_and_the_loop_continues(
    task, database_root, tmp_path
) -> None:
    """The one thing A0 structurally cannot do, and what 3.4's recovery rate is counted from."""
    client = StubClient(
        ("", [call("execute_sql", sql="SELECT nmae FROM singer")]),
        ("", [call("execute_sql", sql=ANSWER_SQL)]),
        ANSWER_SQL,
    )
    _, row, directory = await run_one(client, task, database_root, tmp_path)
    assert row["status"] == COMPLETE
    assert row["detail"]["solved"] is True
    results = [
        e
        for e in read_transcript(transcript_path(directory, task.task_id))
        if e["kind"] == TOOL_RESULT
    ]
    assert [e["ok"] for e in results] == [False, True]
    assert "no such column" in results[0]["error"]


@pytest.mark.asyncio
async def test_an_unknown_tool_name_does_not_end_the_task(task, database_root, tmp_path) -> None:
    client = StubClient(("", [call("describe_tabel", table="singer")]), ANSWER_SQL)
    _, row, _ = await run_one(client, task, database_root, tmp_path)
    assert row["status"] == COMPLETE
    assert row["detail"]["solved"] is True


# --- the transcript ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_task_produces_a_complete_replayable_transcript_on_disk(
    task, database_root, tmp_path
) -> None:
    """**3.2's acceptance.** The file alone rebuilds what the provider was actually sent.

    Compared against `StubClient.handed` rather than against a second idea of what should
    have been sent, so this fails if the writer and the loop ever disagree.
    """
    client = StubClient(
        ("", [call("list_tables")]),
        ("thinking", [call("describe_table", table="singer")]),
        ("", [call("execute_sql", sql=ANSWER_SQL)]),
        ANSWER_SQL,
    )
    _, row, directory = await run_one(client, task, database_root, tmp_path)
    events = read_transcript(transcript_path(directory, task.task_id))

    assert events[0]["kind"] == START
    assert events[-1]["kind"] == END
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))

    for turn, handed in enumerate(client.handed, start=1):
        assert replay(events, turn=turn) == handed
    assert replay(events) == client.handed[-1] + [Message(role="assistant", content=ANSWER_SQL)]
    assert row["detail"]["transcript"] == f"transcripts/{task.task_id}.jsonl"


@pytest.mark.asyncio
async def test_the_end_event_names_the_path_and_checksums_the_file(
    task, database_root, tmp_path
) -> None:
    client = StubClient(("", [call("list_tables")]), ANSWER_SQL)
    _, row, directory = await run_one(client, task, database_root, tmp_path)
    trajectory = read_trajectories(transcript_path(directory, task.task_id))[0]
    assert trajectory.complete
    assert trajectory.end["outcome"] in TERMINATIONS
    assert trajectory.end["turns"] == row["detail"]["turns"]
    assert trajectory.end["tool_calls"] == row["detail"]["tool_calls"]
    assert len(trajectory.tool_results) == trajectory.end["tool_calls"]


@pytest.mark.asyncio
async def test_a_tool_result_carries_the_call_id_and_not_the_arguments(
    task, database_root, tmp_path
) -> None:
    """One copy of the arguments, in the assistant message. One copy cannot disagree."""
    client = StubClient(("", [call("execute_sql", sql=ANSWER_SQL)]), ANSWER_SQL)
    _, _, directory = await run_one(client, task, database_root, tmp_path)
    events = read_transcript(transcript_path(directory, task.task_id))
    result = next(e for e in events if e["kind"] == TOOL_RESULT)
    assert "arguments" not in result
    assert result["call_id"] == "call-execute_sql"
    assistant = next(e for e in events if e["kind"] == MESSAGE and e["tool_calls"])
    assert assistant["tool_calls"][0]["arguments"] == {"sql": ANSWER_SQL}


@pytest.mark.asyncio
async def test_the_transcript_holds_no_tokens_model_or_latency(
    task, database_root, tmp_path
) -> None:
    """The seam. Those are the ledger's `AttemptRow`, joined on (run_id, task_id, turn).

    A transcript that repeated them would be a second source for a number the ledger already
    holds, and two sources for one number is how they come to disagree.
    """
    client = StubClient(("", [call("list_tables")]), ANSWER_SQL)
    _, _, directory = await run_one(client, task, database_root, tmp_path)
    text = transcript_path(directory, task.task_id).read_text()
    for absent in ("prompt_tokens", "completion_tokens", "latency_s", "gpt-oss", "pool"):
        assert absent not in text


@pytest.mark.asyncio
async def test_the_ledger_records_one_attempt_a_turn_with_the_turn_number(
    task, database_root, tmp_path
) -> None:
    """1.3 gave `AttemptRow` a `turn` field and nothing has used it until now. It is the
    join key between the accounting record and the content record.
    """
    client = StubClient(("", [call("list_tables")]), ("", [call("list_tables")]), ANSWER_SQL)
    _, row, directory = await run_one(client, task, database_root, tmp_path)
    attempts = [r for r in read_rows(directory / "ledger.jsonl") if r["kind"] == "attempt"]
    assert [a["turn"] for a in attempts] == [1, 2, 3]
    assert row["attempts"] == 3
    # Tokens are summed over the trajectory, which is what a per-task cost comparison needs.
    assert row["detail"]["prompt_tokens"] == 1200


@pytest.mark.asyncio
async def test_a_trajectory_that_raised_still_records_how_it_ended(
    task, database_root, tmp_path
) -> None:
    """The one thing a reader must never guess is whether a short file is a short trajectory
    or a truncated one.
    """
    client = StubClient(*[("", [call("list_tables")])] * 6)
    _, _, directory = await run_one(
        client, task, database_root, tmp_path, token_ceiling=500, turn_limit=10
    )
    trajectory = read_trajectories(transcript_path(directory, task.task_id))[0]
    assert trajectory.complete
    assert trajectory.end["outcome"] == BUDGET


def test_a_transcript_with_no_end_event_reads_as_incomplete(tmp_path) -> None:
    """A process killed outright does not get to record its own death, and a reader has to
    be able to see that — the same thing the ledger infers from a missing `run_end`.
    """
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps({"kind": START, "seq": 1, "task_id": "dev-0001"})
        + "\n"
        + json.dumps({"kind": MESSAGE, "seq": 2, "turn": 1, "role": "user", "content": "hi"})
        + "\n"
    )
    (trajectory,) = read_trajectories(path)
    assert not trajectory.complete
    assert trajectory.end is None


def test_a_retried_task_appends_a_second_trajectory_rather_than_destroying_the_first(
    tmp_path,
) -> None:
    """Append-only, the ledger's own discipline. The last bracket stands; the first is
    evidence rather than a second measurement.
    """
    from query_pilot.agents.transcript import TranscriptWriter

    for outcome in (BUDGET, ANSWER):
        with TranscriptWriter(tmp_path, "dev-0001") as writer:
            writer.start(
                run_id="r",
                agent="A1",
                db_id="concert",
                question=QUESTION,
                limits={},
                started_at="now",
            )
            writer.end(outcome=outcome, turns=1, tool_calls=0, ended_at="now")
    path = transcript_path(tmp_path, "dev-0001")
    trajectories = read_trajectories(path)
    assert [t.end["outcome"] for t in trajectories] == [BUDGET, ANSWER]
    # The second writer continues the sequence rather than restarting it. A restart would
    # make `seq` stop ordering the file, which is the one guarantee the format makes.
    assert [e["seq"] for e in read_transcript(path)] == [1, 2, 3, 4]


# --- the limits, and what they admit and reject ----------------------------------------------


def test_a_turn_limit_too_low_to_ever_answer_is_refused(database_root, tmp_path) -> None:
    """One turn cannot both call a tool and answer, so A1 would be A0 with extra steps."""
    with pytest.raises(ValueError, match="turn limit"):
        A1(StubClient(), [], database_root, tmp_path, turn_limit=1)
    with pytest.raises(ValueError, match="tool call limit"):
        A1(StubClient(), [], database_root, tmp_path, tool_call_limit=0)


def test_the_turn_limit_admits_the_trajectory_the_probe_measured() -> None:
    """A limit that a legitimate trajectory hits silently moves the accuracy figure.

    The floor is what the measured model actually needs: it emits one tool call per turn, so
    the limit must cover `list_tables`, the tables it chooses to describe, a sample, a
    query, a repair, and the turn that answers.
    """
    probe = json.loads(TOOL_PROBE.read_text())
    assert probe["calls_per_turn_max"] == 1, "the derivation below assumes one call a turn"
    longest = max(len(p["turns"]) for p in probe["probes"])
    assert longest < TURN_LIMIT
    # Every tool call can have its own turn, plus the turn that answers.
    assert TURN_LIMIT >= TOOL_CALL_LIMIT + 1


def test_the_tool_call_limit_leaves_room_for_the_recovery_cycle() -> None:
    """Recovery rate is the most interesting figure in Phase 3, and a tool-call limit that
    cut the second repair would cap the thing 3.4 exists to measure.
    """
    sizes = json.loads(TOOL_SIZES.read_text())
    widest = max(row["tables"] for row in sizes["databases_measured"])
    # list_tables, four describes on an 11-table schema, two samples, and five execute_sql:
    # a query, an error, a repair, a second error and a second repair.
    assert TOOL_CALL_LIMIT >= 1 + 4 + 2 + 5
    assert widest == 11


def test_the_prompt_ceiling_is_the_committed_arithmetic_and_not_a_choice() -> None:
    """Enforcement of constraint 46, read back from the artifact that measured it.

    A ceiling edited without re-running `scripts/a1_tool_sizes.py` fails here, the same
    guard `docs/sandbox-caps.json` puts on 2.2's caps.
    """
    sizes = json.loads(TOOL_SIZES.read_text())
    assert int(sizes["budget"]["budget_chars"]) == PROMPT_CEILING_CHARS
    assert sizes["budget"]["tpm"] == 8000
    # An exhaustive trajectory on the worst database has to fit inside it with room for the
    # system prompt, the question and the assistant's own messages.
    assert sizes["worst_database"]["exhaustive_chars"] < PROMPT_CEILING_CHARS


def test_no_pair_of_policy_limits_could_replace_the_prompt_ceiling() -> None:
    """Why the fifth path exists, held as arithmetic rather than left in a docstring.

    `TOOL_CALL_LIMIT` results, each at the backstop, is several times the ceiling. Making
    the product safe would mean either a tool-call limit that caps the recovery cycle or a
    backstop below the largest legitimate result — both worse than measuring the
    conversation, which is free.
    """
    from query_pilot.agents.tools import TOOL_RESULT_CHARS

    assert TOOL_CALL_LIMIT * TOOL_RESULT_CHARS > PROMPT_CEILING_CHARS


def test_conversation_chars_counts_the_tool_call_arguments() -> None:
    """A turn whose assistant message is empty still carries an `execute_sql` argument, and
    a count that ignored it would under-read the request by the part that grows fastest.
    """
    plain = [Message(role="assistant", content="")]
    with_call = [
        Message(role="assistant", content="", tool_calls=(call("execute_sql", sql="x" * 500),))
    ]
    assert conversation_chars(plain) == 0
    assert conversation_chars(with_call) > 500
