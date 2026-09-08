"""What a run declares about itself before it starts.

A run is a named, resumable pass over a list of task IDs by one agent. **This package
knows what a run and a task are; the client below it does not, and must not.** The
dependency points one way — `query_pilot.run` imports `query_pilot.client`, never the
reverse — so the client stays general infrastructure with no notion of a run, a task ID,
or a ledger, exactly as it was built in 1.1 and 1.2.

The fingerprint is the guard against a resumed run quietly becoming two experiments in one
file. It covers everything a run declares *except* its ID: the agent, the task list in
order, and the parameters. Resuming an ID whose recorded fingerprint disagrees with the
one being offered raises rather than appending, because the alternative is a ledger whose
first half measured one thing and whose second half measured another, with nothing in the
file saying so.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from query_pilot.client.clock import Clock, SystemClock

#: Tasks in flight at once. Matches `max_concurrency` in `config/providers.toml`, where
#: the number was justified: the quota buckets bind long before it does, and what it
#: actually bounds is how far a token ceiling can be overshot by work already dispatched.
DEFAULT_CONCURRENCY = 8


class RunError(Exception):
    """Base for everything this package raises."""


class RunConfigChanged(RunError):
    """A run ID is being resumed with different parameters than it was started with."""

    def __init__(self, run_id: str, *, recorded: str, offered: str) -> None:
        super().__init__(
            f"run {run_id!r} was started with fingerprint {recorded} and is being resumed "
            f"with {offered}; resuming would put two experiments in one ledger. Start a new "
            f"run ID, or pass allow_config_change=True to record the change deliberately."
        )
        self.run_id = run_id
        self.recorded = recorded
        self.offered = offered


def new_run_id(clock: Clock | None = None, rng: random.Random | None = None) -> str:
    """A sortable, human-readable ID: UTC stamp plus six hex digits.

    The stamp is what makes a directory listing chronological; the suffix is what keeps two
    runs started in the same second apart.
    """
    clock = clock or SystemClock()
    rng = rng or random.Random()
    return f"{clock.now_utc().strftime('%Y%m%d-%H%M%S')}-{rng.randrange(16**6):06x}"


@dataclass(frozen=True, slots=True)
class RunConfig:
    """The declaration a run is held to.

    ``params`` is free-form and goes into the fingerprint whole: a run that changed its
    temperature, its prompt or its turn limit between sessions is not the same run, and
    this is where a later phase says so without this module learning what any of those
    mean.
    """

    run_id: str
    agent: str
    task_ids: tuple[str, ...]
    concurrency: int = DEFAULT_CONCURRENCY
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.agent:
            raise RunError("a run must name its agent")
        if not self.task_ids:
            raise RunError(f"run {self.run_id!r} declares no tasks")
        seen = sorted({t for t in self.task_ids if self.task_ids.count(t) > 1})
        if seen:
            # Two rows for one task ID would collapse under the skip rule, and the run
            # would silently measure it once.
            raise RunError(f"run {self.run_id!r} lists duplicate task IDs: {', '.join(seen)}")
        if self.concurrency < 1:
            raise RunError(f"run {self.run_id!r}: concurrency must be at least 1")

    @classmethod
    def start(
        cls,
        agent: str,
        task_ids: Sequence[str],
        *,
        run_id: str | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        params: Mapping[str, Any] | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> RunConfig:
        """Declare a run, generating an ID when the caller does not supply one."""
        return cls(
            run_id=run_id or new_run_id(clock, rng),
            agent=agent,
            task_ids=tuple(task_ids),
            concurrency=concurrency,
            params=dict(params or {}),
        )

    def declared(self) -> dict[str, Any]:
        """Everything the fingerprint covers. The run ID is identity, not declaration."""
        return {
            "agent": self.agent,
            "task_ids": list(self.task_ids),
            "concurrency": self.concurrency,
            "params": dict(self.params),
        }

    def fingerprint(self) -> str:
        canonical = json.dumps(self.declared(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]
