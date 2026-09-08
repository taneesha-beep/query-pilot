"""Derive a summary from a ledger, and refuse to present an incomplete run as a result.

A summary is the only thing about a run that ever gets committed; the ledger and the
transcripts stay in the gitignored run directory. So this is the file that ends up quoted
in a README, and **an incomplete run must not be able to produce one that reads like a
finished one.**

The refusal has two halves, because one is not enough.

*In the file.* An incomplete summary carries ``"complete": false``, the reason it stopped,
and a plain-English ``warning`` as its first key. More importantly, **every derived rate
is written as the literal string** ``"TBD (run incomplete: <reason>)"`` **instead of a
number.** The label has to survive being copied out of the file, and the only way to
guarantee that is for the number not to be in the file at all. Counts of what actually
happened do stay: those are facts about a run, not results derived from one, and hiding
them would make an interrupted run harder to resume rather than harder to misread.

*In the reader.* :meth:`Summary.require_complete` raises. A caller that wants a figure
has to have asked for one, and asking gets an exception rather than a plausible number.

``TBD`` is this project's placeholder convention, so an incomplete summary reads the same
way as every other not-yet-measured figure in the repository.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from query_pilot.run.config import RunError
from query_pilot.run.guard import IncompleteReason
from query_pilot.run.ledger import COMPLETE, LEDGER_NAME, OK, read_rows

SUMMARY_NAME = "summary.json"

WARNING = (
    "INCOMPLETE RUN. This run stopped at {reason} before finishing its declared tasks. "
    "Nothing in it is a result: {complete} of {declared} tasks have an outcome. Every "
    "derived figure below reads TBD because an incomplete run has none."
)


class IncompleteRun(RunError):
    """Someone asked an unfinished run for a result."""

    def __init__(self, run_id: str | None, reason: str | None) -> None:
        super().__init__(
            f"run {run_id!r} is incomplete ({reason or 'reason not recorded'}) and has no "
            f"result to present. Resume it, or report its counts as counts."
        )
        self.run_id = run_id
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Summary:
    """What a run did. ``complete`` gates everything derived from it."""

    run_id: str | None = None
    agent: str | None = None
    complete: bool = False
    incomplete_reason: str | None = None
    tasks_declared: int = 0
    tasks_complete: int = 0
    tasks_failed: int = 0
    tasks_remaining: int = 0
    attempts: int = 0
    attempts_answered: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    walls: int = 0
    sessions: int = 0
    elapsed_s: float = 0.0
    token_ceiling: int | None = None
    wall_clock_ceiling_s: float | None = None
    error_classes: Mapping[str, int] = field(default_factory=dict)
    attempts_by_model: Mapping[str, int] = field(default_factory=dict)
    #: Free-form, and where Phase 2 hangs execution accuracy. Gated by the same rule.
    metrics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def require_complete(self) -> Summary:
        """The reader's refusal. Returns itself when the run finished, raises when not."""
        if not self.complete:
            raise IncompleteRun(self.run_id, self.incomplete_reason)
        return self

    def result(self) -> Mapping[str, Any]:
        """Derived figures, or an exception. Never a number from an unfinished run."""
        return self.require_complete().derived()

    def derived(self) -> dict[str, Any]:
        """Rates computed from the counts. Strings when the run did not finish."""
        if not self.complete:
            placeholder = f"TBD (run incomplete: {self.incomplete_reason or 'unknown'})"
            return dict.fromkeys(
                ("tokens_per_task", "attempts_per_task", "tasks_per_minute"), placeholder
            )
        answered = self.tasks_complete or 1
        minutes = self.elapsed_s / 60.0
        return {
            "tokens_per_task": round(self.total_tokens / answered, 1),
            "attempts_per_task": round(self.attempts / answered, 2),
            "tasks_per_minute": round(self.tasks_complete / minutes, 2) if minutes else None,
        }

    def as_file(self) -> dict[str, Any]:
        """The committed shape. ``warning`` first, so it is the first thing read."""
        document: dict[str, Any] = {}
        if not self.complete:
            document["warning"] = WARNING.format(
                reason=self.incomplete_reason or "an unrecorded stop",
                complete=self.tasks_complete,
                declared=self.tasks_declared,
            )
        document.update(
            {
                "run_id": self.run_id,
                "agent": self.agent,
                "complete": self.complete,
                "incomplete_reason": self.incomplete_reason,
                "ceilings": {
                    "token_ceiling": self.token_ceiling,
                    "wall_clock_ceiling_s": self.wall_clock_ceiling_s,
                },
                "counts": {
                    "tasks_declared": self.tasks_declared,
                    "tasks_complete": self.tasks_complete,
                    "tasks_failed": self.tasks_failed,
                    "tasks_remaining": self.tasks_remaining,
                    "attempts": self.attempts,
                    "attempts_answered": self.attempts_answered,
                    "quota_walls": self.walls,
                    "sessions": self.sessions,
                    "elapsed_s": round(self.elapsed_s, 3),
                },
                "tokens": {
                    "prompt": self.prompt_tokens,
                    "completion": self.completion_tokens,
                    "total": self.total_tokens,
                },
                "error_classes": dict(self.error_classes),
                "attempts_by_model": dict(self.attempts_by_model),
                "derived": self.derived(),
                "metrics": dict(self.metrics),
            }
        )
        return document


def summarise(path: Path | str) -> Summary:
    """Fold a run's ledger into a summary. Takes the ledger file or the run directory."""
    source = Path(path)
    if source.is_dir():
        source = source / LEDGER_NAME

    run_id = agent = None
    declared: Mapping[str, Any] = {}
    tasks_declared = attempts = answered = prompt = completion = walls = sessions = 0
    elapsed = 0.0
    status: dict[str, str] = {}
    error_classes: Counter[str] = Counter()
    by_model: Counter[str] = Counter()
    last_end: Mapping[str, Any] | None = None
    open_segment = False

    for row in read_rows(source):
        kind = row.get("kind")
        run_id = run_id or row.get("run_id")
        if kind == "run_start":
            sessions += 1
            open_segment = True
            agent = row.get("agent") or agent
            if isinstance(row.get("declared"), dict):
                declared = row["declared"]
                tasks_declared = len(declared.get("task_ids") or ()) or tasks_declared
            tasks_declared = row.get("tasks_declared") or tasks_declared
        elif kind == "run_end":
            open_segment = False
            last_end = row
            elapsed += float(row.get("elapsed_s") or 0.0)
        elif kind == "attempt":
            attempts += 1
            prompt += row.get("prompt_tokens") or 0
            completion += row.get("completion_tokens") or 0
            if row.get("outcome") == OK:
                answered += 1
            else:
                error_classes[str(row.get("error_class") or "unclassified")] += 1
            by_model[f"{row.get('provider')}/{row.get('model')}[{row.get('pool')}]"] += 1
        elif kind == "task":
            task_id = row.get("task_id")
            if isinstance(task_id, str):
                status[task_id] = str(row.get("status", ""))
        elif kind == "wall":
            walls += 1

    complete_ids = {t for t, s in status.items() if s == COMPLETE}
    if open_segment:
        # No `run_end` on the last segment: the process was killed outright and could not
        # write one. Its absence is the record, and it is never a finished run.
        reason: str | None = IncompleteReason.KILLED.value
        complete = False
    elif last_end is None:
        reason, complete = IncompleteReason.KILLED.value, False
    else:
        reason = last_end.get("incomplete_reason")
        complete = last_end.get("status") == "complete" and reason is None

    return Summary(
        run_id=run_id,
        agent=agent,
        complete=complete,
        incomplete_reason=reason,
        tasks_declared=tasks_declared,
        tasks_complete=len(complete_ids),
        tasks_failed=len(set(status) - complete_ids),
        tasks_remaining=max(0, tasks_declared - len(complete_ids)),
        attempts=attempts,
        attempts_answered=answered,
        prompt_tokens=prompt,
        completion_tokens=completion,
        walls=walls,
        sessions=sessions,
        # Killed segments contribute nothing: a process that could not write `run_end`
        # could not write how long it had been running either.
        elapsed_s=elapsed,
        token_ceiling=declared.get("token_ceiling"),
        wall_clock_ceiling_s=declared.get("wall_clock_ceiling_s"),
        error_classes=dict(error_classes),
        attempts_by_model=dict(by_model),
    )


def write_summary(summary: Summary, path: Path | str) -> Path:
    """Write the summary beside its ledger. Takes the file or the run directory."""
    target = Path(path)
    if target.is_dir() or not target.suffix:
        target = target / SUMMARY_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary.as_file(), indent=2) + "\n")
    return target


def read_summary(path: Path | str) -> Summary:
    """Read a written summary back. The label survives the round trip."""
    source = Path(path)
    if source.is_dir():
        source = source / SUMMARY_NAME
    document = json.loads(source.read_text())
    counts = document.get("counts") or {}
    tokens = document.get("tokens") or {}
    ceilings = document.get("ceilings") or {}
    return Summary(
        run_id=document.get("run_id"),
        agent=document.get("agent"),
        complete=bool(document.get("complete")),
        incomplete_reason=document.get("incomplete_reason"),
        tasks_declared=counts.get("tasks_declared", 0),
        tasks_complete=counts.get("tasks_complete", 0),
        tasks_failed=counts.get("tasks_failed", 0),
        tasks_remaining=counts.get("tasks_remaining", 0),
        attempts=counts.get("attempts", 0),
        attempts_answered=counts.get("attempts_answered", 0),
        prompt_tokens=tokens.get("prompt", 0),
        completion_tokens=tokens.get("completion", 0),
        walls=counts.get("quota_walls", 0),
        sessions=counts.get("sessions", 0),
        elapsed_s=counts.get("elapsed_s", 0.0),
        token_ceiling=ceilings.get("token_ceiling"),
        wall_clock_ceiling_s=ceilings.get("wall_clock_ceiling_s"),
        error_classes=document.get("error_classes") or {},
        attempts_by_model=document.get("attempts_by_model") or {},
        metrics=document.get("metrics") or {},
    )
