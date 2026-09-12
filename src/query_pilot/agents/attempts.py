"""What each request of an A1-shaped run sent and cost, measured the way the prompt ceiling counts.

**Why this exists: constraint 64 rests on one line of arithmetic that was never checked against
A1's own requests.** ``PROMPT_CEILING_CHARS / 3.265 + MAX_OUTPUT_TOKENS`` = 8,000 tokens, the
endpoint's whole per-minute budget, is what forces every A1 run to concurrency 1 — and 3.265
characters per token was measured on **A0's** prompts (`docs/a0-prompt-sizes.json`): a rendered
schema and a question, no tool schemas, no tool results. An A1 request carries both.

**The measurement.** Every request a trajectory made is paired with the conversation it sent:
the transcript is replayed to that turn (:func:`~query_pilot.agents.transcript.replay`) and
counted by :func:`~query_pilot.agents.a1.conversation_chars` — **the exact quantity**
``PROMPT_CEILING_CHARS`` **bounds** — against the prompt tokens the provider reported for that
request in the ledger's attempt row, joined on ``(task_id, turn)``. The tool schemas are sent
with every discovery turn and are in the provider's count but not in the characters, so the
ratio here is lower than a pure text ratio; that is the point, because the ceiling is enforced
on the characters and the bucket is charged the tokens.

**The last bracket stands** (constraint 86): a retried task's earlier requests are paid for and
in the ledger, but they are not paired here, because the conversation they sent is an earlier
trajectory's. So ``attempts_paired`` is at most the run's request count, and is reported beside
it rather than presented as it.

Two readings are made, both reported, neither preferred silently: the per-request ratio's spread,
and a least-squares line ``prompt_tokens = intercept + chars / chars_per_token`` whose value at
the ceiling is the attempt constraint 64 is about. **Nothing here moves a limit.** Every A1
limit is frozen underneath 3.6 (constraint 72), and a cheap run holds them exactly so that the
model is the only difference.

:func:`loop_behaviour` sits here too, because it is the same kind of fact read off the same two
files: how many tool calls one assistant message carried, and why each request stopped
generating. Constraint 57's "a turn is a tool call" was measured on ``openai/gpt-oss-120b``, and
a run on any other model owes the same count.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from query_pilot.agents.a1 import MAX_OUTPUT_TOKENS, PROMPT_CEILING_CHARS, conversation_chars
from query_pilot.agents.metrics import TBD
from query_pilot.agents.transcript import (
    MESSAGE,
    TRANSCRIPTS_DIR,
    read_trajectories,
    replay,
)
from query_pilot.run.ledger import LEDGER_NAME, read_rows

__all__ = [
    "AttemptSize",
    "loop_behaviour",
    "read_attempt_sizes",
    "summarise_sizes",
]

_OK = "ok"


@dataclass(frozen=True, slots=True)
class AttemptSize:
    """One request: what it sent, in characters, and what the provider charged for it."""

    task_id: str
    turn: int
    chars: int
    prompt_tokens: int
    completion_tokens: int
    provider: str
    model: str
    pool: str
    finish_reason: str | None

    @property
    def tokens(self) -> int:
        """What the per-minute bucket is charged for this request."""
        return self.prompt_tokens + self.completion_tokens


def _ok_attempts(ledger: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """The last successful attempt row per ``(task_id, turn)``.

    Last, because a retried task's second bracket reuses the first one's turn numbers and comes
    after it in the ledger; successful, because a refused attempt charged no tokens.
    """
    rows: dict[tuple[str, int], dict[str, Any]] = {}
    for row in read_rows(ledger):
        if row.get("kind") != "attempt" or row.get("outcome") != _OK:
            continue
        if row.get("prompt_tokens") is None or row.get("turn") is None:
            continue
        rows[(str(row["task_id"]), int(row["turn"]))] = row
    return rows


def _assistant_turns(events: Sequence[Any]) -> list[int]:
    return sorted(
        {int(e["turn"]) for e in events if e["kind"] == MESSAGE and e["role"] == "assistant"}
    )


def read_attempt_sizes(run_directory: Path | str) -> tuple[AttemptSize, ...]:
    """Pair every request of every task's last bracket with the conversation it sent."""
    directory = Path(run_directory)
    attempts = _ok_attempts(directory / LEDGER_NAME)
    sizes: list[AttemptSize] = []
    for path in sorted((directory / TRANSCRIPTS_DIR).glob("*.jsonl")):
        trajectories = read_trajectories(path)
        if not trajectories:
            continue
        trajectory = trajectories[-1]
        for turn in _assistant_turns(trajectory.events):
            row = attempts.get((trajectory.task_id, turn))
            if row is None:
                continue
            sizes.append(
                AttemptSize(
                    task_id=trajectory.task_id,
                    turn=turn,
                    chars=conversation_chars(replay(trajectory.events, turn=turn)),
                    prompt_tokens=int(row["prompt_tokens"]),
                    completion_tokens=int(row.get("completion_tokens") or 0),
                    provider=str(row.get("provider")),
                    model=str(row.get("model")),
                    pool=str(row.get("pool")),
                    finish_reason=row.get("finish_reason"),
                )
            )
    return tuple(sizes)


def summarise_sizes(
    sizes: Sequence[AttemptSize],
    *,
    tpm: int,
    prompt_ceiling_chars: int = PROMPT_CEILING_CHARS,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
) -> dict[str, Any]:
    """The spread, the line, the largest request actually made, and what the line says at the
    ceiling. ``TBD`` wherever there is too little to compute from, never 0."""
    if not sizes:
        return {"attempts_paired": 0, "tpm": tpm}
    ratios = [size.chars / size.prompt_tokens for size in sizes if size.prompt_tokens]
    largest = max(sizes, key=lambda size: size.tokens)
    longest = max(sizes, key=lambda size: size.chars)

    fit: dict[str, Any]
    if len({size.chars for size in sizes}) < 2:
        fit = {"intercept_tokens": TBD, "chars_per_token": TBD}
        projected: Any = TBD
    else:
        line = statistics.linear_regression(
            [size.chars for size in sizes], [size.prompt_tokens for size in sizes]
        )
        fit = {
            "intercept_tokens": round(line.intercept, 1),
            "chars_per_token": round(1 / line.slope, 3),
        }
        projected = round(line.intercept + line.slope * prompt_ceiling_chars + max_output_tokens)

    return {
        "attempts_paired": len(sizes),
        "served_by": dict(Counter(f"{size.provider} {size.model}" for size in sizes)),
        "pools": dict(Counter(size.pool for size in sizes)),
        "chars_per_prompt_token": {
            "min": round(min(ratios), 3),
            "median": round(statistics.median(ratios), 3),
            "max": round(max(ratios), 3),
        },
        "fit": fit,
        "largest_attempt": {
            "tokens": largest.tokens,
            "prompt_tokens": largest.prompt_tokens,
            "completion_tokens": largest.completion_tokens,
            "chars": largest.chars,
            "task_id": largest.task_id,
            "turn": largest.turn,
            "share_of_tpm": round(largest.tokens / tpm, 4),
        },
        "longest_conversation": {
            "chars": longest.chars,
            "task_id": longest.task_id,
            "turn": longest.turn,
            "share_of_ceiling": round(longest.chars / prompt_ceiling_chars, 4),
        },
        "largest_completion_tokens": max(size.completion_tokens for size in sizes),
        "attempts_over_tpm": sum(1 for size in sizes if size.tokens > tpm),
        "projected_attempt_at_ceiling": projected,
        "tpm": tpm,
        "prompt_ceiling_chars": prompt_ceiling_chars,
        "max_output_tokens": max_output_tokens,
    }


def loop_behaviour(run_directory: Path | str) -> dict[str, Any]:
    """How the model drove the loop: calls per assistant message, and why each request stopped.

    Read over every task's last bracket. ``finish_reasons`` counts the successful attempt rows
    paired to those brackets, so it covers the same requests :func:`read_attempt_sizes` does.
    """
    directory = Path(run_directory)
    per_message: Counter[int] = Counter()
    for path in sorted((directory / TRANSCRIPTS_DIR).glob("*.jsonl")):
        trajectories = read_trajectories(path)
        if not trajectories:
            continue
        for event in trajectories[-1].events:
            if event["kind"] == MESSAGE and event["role"] == "assistant":
                per_message[len(event.get("tool_calls") or ())] += 1
    finish = Counter(str(size.finish_reason) for size in read_attempt_sizes(directory))
    return {
        "assistant_messages": sum(per_message.values()),
        "tool_calls_per_assistant_message": {
            str(calls): count for calls, count in sorted(per_message.items())
        },
        "max_tool_calls_in_one_message": max(per_message, default=0),
        "messages_with_more_than_one_call": sum(
            count for calls, count in per_message.items() if calls > 1
        ),
        "finish_reasons": dict(sorted(finish.items())),
    }
