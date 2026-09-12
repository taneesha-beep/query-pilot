"""Every message, tool call and tool result of one trajectory, in order, on disk.

**The transcript is the product of 3.2 and it has three readers, none of which is a
person.** 3.4 computes every trajectory metric from it, Phase 4.3 reads compliance out of
it — whether a trajectory *attempted* an injected instruction, which the final answer
cannot show — and Phase 7.2's viewer renders it as the ordered list of steps a person
watches. It is shaped for those three, and a reshape after 3.6 means re-running a measured
run, which is why the format is decided here rather than grown.

**What replayable means, exactly.** From the file alone you can reconstruct the ordered
``list[Message]`` that was sent to the provider at every turn: the conversation at turn *n*
is every ``message`` event with ``turn <= n``, in ``seq`` order, up to and including that
turn's assistant message. :func:`replay` does it and a test asserts the result is identical
to what the client was actually handed. That is the guarantee; anything else here is
convenience on top of it.

**What it does not hold, and this is the seam.** No token counts, no model string, no
latency, no provider. Those are the run ledger's :class:`~query_pilot.run.ledger.AttemptRow`,
one per provider call, and 1.3 gave it a ``turn`` field that has been unused since. The
join key is ``(run_id, task_id, turn)``. **The ledger is the accounting record, the
transcript is the content record, and a task row's ``detail`` is the outcome record** —
three files, no duplication, each answering what the other two deliberately do not. The one
repetition is the ``end`` event's two counts, which are a checksum on the file the way
``run_end``'s totals are a checksum on the ledger, not a second source.

**One file per task, append-only, JSONL.** Per task because 7.2 renders one trajectory and
should open one file, and because 3.4 iterating is a directory listing rather than a
group-by over a 150-task file. Append-only because that is the ledger's own discipline and
it buys the same two things: a killed process leaves a partial trajectory rather than
nothing, and a resumed run's retry appends a second bracket instead of destroying the
first. **Order is carried by ``seq``, not by file position**, so a future one-file-per-run
layout is a change of writer and not of reader — and so a trajectory stays ordered even if
a later phase runs a turn's tool calls concurrently, which 3.2 deliberately does not.

Four event kinds and nothing else:

``start``   the header: task, database, question, the limits in force.
``message`` one entry of the conversation, exactly as it goes to the provider.
``tool_result``  what one call actually did — the record 3.4 and 4.3 read. It carries the
            ``call_id`` and not the arguments, which live once, in the assistant message
            that made the call.
``end``     which of the termination paths ended it, and the two counts.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final

from query_pilot.client.types import Message, ToolCall

__all__ = [
    "END",
    "MESSAGE",
    "START",
    "TOOL_RESULT",
    "TRANSCRIPTS_DIR",
    "Trajectory",
    "TranscriptWriter",
    "read_trajectories",
    "read_transcript",
    "replay",
    "transcript_path",
]

#: Beside `ledger.jsonl` in the run directory, which `.gitignore` already covers.
TRANSCRIPTS_DIR: Final = "transcripts"

START: Final = "start"
MESSAGE: Final = "message"
TOOL_RESULT: Final = "tool_result"
END: Final = "end"


def transcript_path(run_directory: Path | str, task_id: str) -> Path:
    """Where one task's trajectory lives. One task, one file, beside the ledger."""
    return Path(run_directory) / TRANSCRIPTS_DIR / f"{task_id}.jsonl"


def _message_row(message: Message) -> dict[str, Any]:
    """A :class:`Message` as JSON, losing nothing :func:`replay` needs to rebuild it.

    ``tool_call_id`` and ``name`` are both kept on a tool result even though each provider
    uses only one of them — the OpenAI-shaped API addresses a result by id and Google's by
    the function's name. A transcript that kept one would replay to one provider and not
    the other, which is the difference `client/types.py` exists to erase.
    """
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
            for call in message.tool_calls
        ],
        "tool_call_id": message.tool_call_id,
        "name": message.name,
    }


def _message_from(row: Mapping[str, Any]) -> Message:
    return Message(
        role=row["role"],
        content=row.get("content") or "",
        tool_calls=tuple(
            ToolCall(id=call["id"], name=call["name"], arguments=dict(call.get("arguments") or {}))
            for call in row.get("tool_calls") or ()
        ),
        tool_call_id=row.get("tool_call_id"),
        name=row.get("name"),
    )


def _last_seq(path: Path) -> int:
    """The highest ``seq`` already in a transcript, or 0. See :class:`TranscriptWriter`."""
    if not path.exists():
        return 0
    return max(
        (
            json.loads(line)["seq"]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ),
        default=0,
    )


class TranscriptWriter:
    """Appends one task's events as they happen. **Never rewrites and never buffers.**

    Flushed and fsynced per event, for the reason 1.3's ledger is: a row that exists only
    in a buffer is a row a killed process throws away, and a trajectory cut off mid-turn is
    the one a person most wants to read. At A1's sizes this is a few events a task.

    Locked because a run may execute tasks concurrently. Each writer owns one file, so the
    lock is only ever contended by a future loop that ran a turn's tool calls in parallel —
    which 3.2 does not do, and which this stays correct under.
    """

    def __init__(self, run_directory: Path | str, task_id: str) -> None:
        self.path = transcript_path(run_directory, task_id)
        self.task_id = task_id
        # **Continued, not restarted.** A task that failed and is retried on resume opens a
        # second writer on the same file, and a second sequence starting at 1 would make
        # `seq` stop ordering the file — which is the one guarantee this format makes, and
        # the thing every reader here sorts on.
        self.seq = _last_seq(self.path)
        self._handle: IO[str] | None = None
        self._lock = threading.Lock()

    def __enter__(self) -> TranscriptWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    def _append(self, kind: str, row: Mapping[str, Any]) -> None:
        with self._lock:
            if self._handle is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = self.path.open("a", encoding="utf-8")
            self.seq += 1
            line = json.dumps({"kind": kind, "seq": self.seq, **row}, default=str) + "\n"
            self._handle.write(line)
            self._handle.flush()
            os.fsync(self._handle.fileno())

    def start(
        self,
        *,
        run_id: str,
        agent: str,
        db_id: str,
        question: str,
        limits: Mapping[str, Any],
        started_at: str,
    ) -> None:
        """Open a trajectory. A retried task appends a second one to the same file.

        The limits are written here rather than left to the run declaration because a
        transcript is read on its own by 3.4 and 7.2, and a trajectory that stopped at a
        limit is unreadable without knowing what the limit was.
        """
        self._append(
            START,
            {
                "run_id": run_id,
                "task_id": self.task_id,
                "agent": agent,
                "db_id": db_id,
                "question": question,
                "limits": dict(limits),
                "started_at": started_at,
            },
        )

    def message(self, message: Message, *, turn: int, repair: bool = False) -> None:
        """One entry of the conversation, recorded as it is appended to it.

        Recorded on the way in rather than at the end of the turn, so that the order on
        disk is the order the conversation was built in and a trajectory killed mid-turn
        still shows what the model had said.

        ``repair`` marks the two messages of 3.3's repair turn. **Added in 3.3, which is
        before 3.6 and therefore while adding a field is still free.** It exists so that the
        repair counts on the ``end`` event stay what every other number there is — a
        checksum over facts the events already carry — rather than becoming the only place a
        reader could learn a repair happened. Identifying the turn by the wording of its
        request instead would tie every reader to a prompt string.
        """
        self._append(MESSAGE, {"turn": turn, "repair": repair, **_message_row(message)})

    def tool_result(
        self,
        *,
        turn: int,
        call_id: str,
        name: str,
        ok: bool,
        error: str | None = None,
        rows_returned: int | None = None,
        rows_shown: int | None = None,
        truncated_by: str | None = None,
        elapsed_s: float | None = None,
    ) -> None:
        """What one call did. **The arguments are not here** — they are in the assistant
        message that made the call, found by ``call_id``, and one copy cannot disagree with
        itself.
        """
        self._append(
            TOOL_RESULT,
            {
                "turn": turn,
                "call_id": call_id,
                "name": name,
                "ok": ok,
                "error": error,
                "rows_returned": rows_returned,
                "rows_shown": rows_shown,
                "truncated_by": truncated_by,
                "elapsed_s": elapsed_s,
            },
        )

    def end(
        self,
        *,
        outcome: str,
        turns: int,
        tool_calls: int,
        ended_at: str,
        repair_attempts: int = 0,
        repair_succeeded: bool = False,
        repair_blocked: str | None = None,
        failed_generation: str | None = None,
    ) -> None:
        """Close a trajectory, naming which termination path ended it.

        The counts are derivable from the events above and are written anyway, as a
        checksum a reader can use to tell a complete file from a truncated one — the same
        job `run_end`'s totals do for the ledger.

        **The three repair fields are 3.3's, and they hold to the same rule.**
        ``repair_attempts`` is the number of messages flagged ``repair`` divided by two, and
        ``repair_succeeded`` is whether the reply that followed passed validation — both
        checkable against the events rather than believed. ``repair_blocked`` is the one
        thing here that is *not* derivable, and it is the reason the trio is worth writing:
        without it a trajectory that was owed a repair and did not get one is
        indistinguishable from one that never needed a repair at all.

        **``failed_generation`` is 5.1's, added 2026-09-12, and written only when present**, so
        every other trajectory's ``end`` keeps the shape 3.6's transcripts have. It is what the
        model generated on the request the provider refused: content, like every message in
        this file, which is why it is here and the provider's code and message are in the
        ledger's ``detail`` instead. It is never a ``message`` event, because it was never
        appended to the conversation — :func:`replay` must still rebuild only what was sent.
        """
        row: dict[str, Any] = {
            "outcome": outcome,
            "turns": turns,
            "tool_calls": tool_calls,
            "repair_attempts": repair_attempts,
            "repair_succeeded": repair_succeeded,
            "repair_blocked": repair_blocked,
            "ended_at": ended_at,
        }
        if failed_generation is not None:
            row["failed_generation"] = failed_generation
        self._append(END, row)


# --- reading -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Trajectory:
    """One bracketed start-to-end stretch of a transcript.

    A task run once has one. A task that failed and was retried on resume has two, and the
    **last is the one that stands** — the same rule the ledger's segments follow, and for
    the same reason: the earlier attempt is evidence, not a second measurement.

    ``end`` is ``None`` for a trajectory whose process was killed before it could record
    its own death, which is exactly what a reader should be able to see.
    """

    start: Mapping[str, Any]
    events: tuple[Mapping[str, Any], ...]
    end: Mapping[str, Any] | None = None

    @property
    def task_id(self) -> str:
        return str(self.start["task_id"])

    @property
    def messages(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(event for event in self.events if event["kind"] == MESSAGE)

    @property
    def tool_results(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(event for event in self.events if event["kind"] == TOOL_RESULT)

    @property
    def complete(self) -> bool:
        """Whether the process lived long enough to close the file."""
        return self.end is not None


def read_transcript(path: Path | str) -> tuple[Mapping[str, Any], ...]:
    """Every event of one file, in ``seq`` order.

    Sorted rather than trusted to file order: ``seq`` is the ordering guarantee this format
    makes, and a reader that relied on position would quietly stop being correct the day a
    turn's tool calls ran concurrently or the layout became one file per run.
    """
    path = Path(path)
    if not path.exists():
        return ()
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return tuple(sorted(events, key=lambda event: event["seq"]))


def read_trajectories(path: Path | str) -> tuple[Trajectory, ...]:
    """Split a transcript into its start-to-end brackets, in order."""
    trajectories: list[Trajectory] = []
    start: Mapping[str, Any] | None = None
    events: list[Mapping[str, Any]] = []
    for event in read_transcript(path):
        if event["kind"] == START:
            if start is not None:
                trajectories.append(Trajectory(start, tuple(events)))
            start, events = event, []
        elif event["kind"] == END:
            if start is not None:
                trajectories.append(Trajectory(start, tuple(events), event))
                start, events = None, []
        elif start is not None:
            events.append(event)
    if start is not None:
        trajectories.append(Trajectory(start, tuple(events)))
    return tuple(trajectories)


def replay(events: Iterable[Mapping[str, Any]], *, turn: int | None = None) -> list[Message]:
    """Rebuild the conversation that was sent to the provider.

    **This is the replayability guarantee, and it is one function so a test can hold it.**
    With no ``turn``, the whole conversation; with one, the conversation as it stood when
    that turn's request was made — which is every message recorded before that turn's own
    assistant message, and is what Phase 7.2 needs to show a trajectory one step at a time.
    """
    messages = []
    for event in events:
        if event["kind"] != MESSAGE:
            continue
        if turn is not None and event["turn"] > turn:
            break
        if turn is not None and event["turn"] == turn and event["role"] == "assistant":
            break
        messages.append(_message_from(event))
    return messages
