"""What each request sent and cost: the measurement constraint 64 was corrected by.

Every run directory here is built with the real transcript writer and a hand-written ledger,
so the join the reader makes — a request's ``(task_id, turn)`` against the conversation that
turn sent — is checked against numbers chosen to make it exact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from query_pilot.agents.a1 import MAX_OUTPUT_TOKENS, PROMPT_CEILING_CHARS, conversation_chars
from query_pilot.agents.attempts import (
    AttemptSize,
    loop_behaviour,
    read_attempt_sizes,
    summarise_sizes,
)
from query_pilot.agents.transcript import TranscriptWriter
from query_pilot.client.types import Message, ToolCall

RUN_ID = "20260912-000000-cccccc"
STAMP = "2026-09-12T00:00:00+00:00"
SYSTEM = Message(role="system", content="s" * 100)
QUESTION = Message(role="user", content="q" * 50)


def _attempt(task_id: str, turn: int, prompt: int, completion: int = 10, **extra) -> dict:
    return {
        "kind": "attempt",
        "run_id": RUN_ID,
        "task_id": task_id,
        "turn": turn,
        "outcome": extra.pop("outcome", "ok"),
        "provider": "groq",
        "model": "openai/gpt-oss-20b",
        "pool": extra.pop("pool", "groq#1"),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "finish_reason": extra.pop("finish_reason", "tool_calls"),
        "recorded_at": STAMP,
        **extra,
    }


def _bracket(directory: Path, task_id: str, assistants: list[Message], outcome: str = "answer"):
    """One bracket whose turn-n assistant message is ``assistants[n-1]``, each followed by a
    tool result for every call it made."""
    writer = TranscriptWriter(directory, task_id)
    writer.start(
        run_id=RUN_ID, agent="A2-cheap", db_id="db", question="q", limits={}, started_at=STAMP
    )
    writer.message(SYSTEM, turn=0)
    writer.message(QUESTION, turn=0)
    calls = 0
    for turn, message in enumerate(assistants, start=1):
        writer.message(message, turn=turn)
        for call in message.tool_calls:
            writer.message(
                Message(role="tool", content="r" * 30, tool_call_id=call.id, name=call.name),
                turn=turn,
            )
            writer.tool_result(turn=turn, call_id=call.id, name=call.name, ok=True)
            calls += 1
    writer.end(outcome=outcome, turns=len(assistants), tool_calls=calls, ended_at=STAMP)
    writer.close()


def _ledger(directory: Path, rows: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


LIST = ToolCall(id="c1", name="list_tables", arguments={})


@pytest.fixture
def run_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "run"
    calling = Message(role="assistant", content="", tool_calls=(LIST,))
    answering = Message(role="assistant", content="SELECT 1")
    _bracket(directory, "t-a", [calling, answering])
    # Retried: the first bracket reached turn 1 only; the second is the one that stands.
    _bracket(directory, "t-b", [calling], outcome="error")
    _bracket(directory, "t-b", [calling, answering])
    _ledger(
        directory,
        [
            _attempt("t-a", 1, 60),
            _attempt("t-a", 2, 90, finish_reason="stop"),
            _attempt("t-b", 1, 999, pool="groq#2"),  # the first bracket's, superseded
            _attempt("t-b", 1, 61, outcome="quota_minute"),  # refused: charged nothing
            _attempt("t-b", 1, 62),
            _attempt("t-b", 2, 95, finish_reason="stop"),
        ],
    )
    return directory


def test_each_request_is_paired_with_the_conversation_it_sent(run_directory) -> None:
    sizes = {(s.task_id, s.turn): s for s in read_attempt_sizes(run_directory)}
    assert set(sizes) == {("t-a", 1), ("t-a", 2), ("t-b", 1), ("t-b", 2)}
    # Turn 1 sent the opening two messages; turn 2 also sent turn 1's call and its result.
    assert sizes[("t-a", 1)].chars == conversation_chars([SYSTEM, QUESTION]) == 150
    tool_call_chars = len("list_tables") + len("{}")
    assert sizes[("t-a", 2)].chars == 150 + tool_call_chars + 30
    assert sizes[("t-a", 2)].tokens == 100


def test_the_last_successful_row_for_a_turn_is_the_one_paired(run_directory) -> None:
    """A retried task reuses turn numbers; the superseded bracket's row and a refused row
    must not be what a turn is measured by."""
    sizes = {(s.task_id, s.turn): s for s in read_attempt_sizes(run_directory)}
    assert sizes[("t-b", 1)].prompt_tokens == 62
    assert sizes[("t-b", 1)].pool == "groq#1"


def test_the_summary_reports_the_spread_the_line_and_the_largest_request(run_directory) -> None:
    summary = summarise_sizes(read_attempt_sizes(run_directory), tpm=8000)
    assert summary["attempts_paired"] == 4
    assert summary["served_by"] == {"groq openai/gpt-oss-20b": 4}
    assert summary["largest_attempt"]["tokens"] == 105
    assert summary["largest_attempt"]["task_id"] == "t-b"
    assert summary["attempts_over_tpm"] == 0
    assert summary["prompt_ceiling_chars"] == PROMPT_CEILING_CHARS
    assert summary["max_output_tokens"] == MAX_OUTPUT_TOKENS
    fit = summary["fit"]
    projected = fit["intercept_tokens"] + PROMPT_CEILING_CHARS / fit["chars_per_token"]
    assert abs(projected + MAX_OUTPUT_TOKENS - summary["projected_attempt_at_ceiling"]) <= 2


def test_a_request_over_the_per_minute_budget_is_counted() -> None:
    big = AttemptSize("t", 1, 20_000, 7_900, 200, "groq", "m", "groq#1", "stop")
    small = AttemptSize("t", 2, 1_000, 400, 20, "groq", "m", "groq#1", "stop")
    summary = summarise_sizes([big, small], tpm=8000)
    assert summary["attempts_over_tpm"] == 1
    assert summary["largest_attempt"]["share_of_tpm"] == round(8_100 / 8000, 4)


def test_a_line_through_one_point_is_tbd_rather_than_invented() -> None:
    one = AttemptSize("t", 1, 1_000, 400, 20, "groq", "m", "groq#1", "stop")
    summary = summarise_sizes([one, one], tpm=8000)
    assert summary["fit"] == {"intercept_tokens": "TBD", "chars_per_token": "TBD"}
    assert summary["projected_attempt_at_ceiling"] == "TBD"
    assert summarise_sizes([], tpm=8000) == {"attempts_paired": 0, "tpm": 8000}


def test_loop_behaviour_counts_calls_per_assistant_message_over_the_last_bracket(
    run_directory, tmp_path
) -> None:
    behaviour = loop_behaviour(run_directory)
    assert behaviour["assistant_messages"] == 4
    assert behaviour["tool_calls_per_assistant_message"] == {"0": 2, "1": 2}
    assert behaviour["max_tool_calls_in_one_message"] == 1
    assert behaviour["messages_with_more_than_one_call"] == 0
    assert behaviour["finish_reasons"] == {"stop": 2, "tool_calls": 2}

    # A model that batches is visible here, which is what constraint 57 needs from any model.
    batched = tmp_path / "batched"
    two = Message(
        role="assistant",
        content="",
        tool_calls=(LIST, ToolCall(id="c2", name="describe_table", arguments={"table": "x"})),
    )
    _bracket(batched, "t-c", [two, Message(role="assistant", content="SELECT 1")])
    _ledger(batched, [_attempt("t-c", 1, 60), _attempt("t-c", 2, 90, finish_reason="stop")])
    behaviour = loop_behaviour(batched)
    assert behaviour["max_tool_calls_in_one_message"] == 2
    assert behaviour["messages_with_more_than_one_call"] == 1
