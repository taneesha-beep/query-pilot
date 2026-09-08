"""What a run declares about itself before it starts.

A run is a named, resumable pass over a list of task IDs by one agent. **This package
knows what a run and a task are; the client below it does not, and must not.** The
dependency points one way — `query_pilot.run` imports `query_pilot.client`, never the
reverse — so the client stays general infrastructure with no notion of a run, a task ID,
or a ledger, exactly as it was built in 1.1 and 1.2.

The fingerprint is the guard against a resumed run quietly becoming two experiments in one
file. It covers everything a run declares *except* its ID: the agent, the task list in
order, the ceilings and the parameters. Resuming an ID whose recorded fingerprint
disagrees with the one being offered raises rather than appending, because the alternative
is a ledger whose first half measured one thing and whose second half measured another,
with nothing in the file saying so. **The ceilings are inside the fingerprint on purpose**
— raising a budget mid-run is precisely the change that must not happen quietly.

**Both ceilings are required and there is no way to construct a run without them.** The
roadmap says every run declares a token ceiling and a wall-clock ceiling before it starts;
an optional field with a sensible default would be a run that declared neither while
looking like it had.
"""

from __future__ import annotations

import hashlib
import json
import random
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
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
    #: Total tokens this run may spend, cumulative across every resume of this run ID.
    #: Traced to a measured figure in docs/PROVIDERS.md; see config/runs/.
    token_ceiling: int
    #: Seconds this run may spend in **one session**. A run spanning a quota reset spends
    #: most of its calendar time not running, and a ceiling that counted those hours would
    #: abort it for waiting.
    wall_clock_ceiling_s: float
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
        if self.token_ceiling <= 0:
            raise RunError(f"run {self.run_id!r}: token_ceiling must be positive")
        if self.wall_clock_ceiling_s <= 0:
            raise RunError(f"run {self.run_id!r}: wall_clock_ceiling_s must be positive")

    @classmethod
    def start(
        cls,
        agent: str,
        task_ids: Sequence[str],
        *,
        token_ceiling: int,
        wall_clock_ceiling_s: float,
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
            token_ceiling=token_ceiling,
            wall_clock_ceiling_s=wall_clock_ceiling_s,
            concurrency=concurrency,
            params=dict(params or {}),
        )

    @classmethod
    def load(
        cls,
        path: Path | str,
        task_ids: Sequence[str],
        *,
        run_id: str | None = None,
        agent: str | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> RunConfig:
        """Read a committed run declaration, and take the task list from the caller.

        The ceilings live in the committed file because the roadmap requires them to be
        declared before a run starts and reviewable afterwards. The **task IDs do not**:
        they come from `splits/`, which is the single committed source of who is in the
        working set, and duplicating them into a second file is how the two drift apart
        without either admitting it.
        """
        source = Path(path)
        try:
            raw = tomllib.loads(source.read_text())
        except FileNotFoundError as exc:
            raise RunError(f"no run configuration at {source}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise RunError(f"{source}: {exc}") from exc

        run = raw.get("run")
        if not isinstance(run, dict):
            raise RunError(f"{source}: needs a [run] table")
        for required in ("agent", "token_ceiling", "wall_clock_ceiling_s"):
            if required not in run:
                raise RunError(f"{source}: [run] is missing {required!r}")
        return cls.start(
            agent if agent is not None else str(run["agent"]),
            task_ids,
            token_ceiling=int(run["token_ceiling"]),
            wall_clock_ceiling_s=float(run["wall_clock_ceiling_s"]),
            run_id=run_id,
            concurrency=int(run.get("concurrency", DEFAULT_CONCURRENCY)),
            params=dict(run.get("params") or {}),
            clock=clock,
            rng=rng,
        )

    def declared(self) -> dict[str, Any]:
        """Everything the fingerprint covers. The run ID is identity, not declaration."""
        return {
            "agent": self.agent,
            "task_ids": list(self.task_ids),
            "token_ceiling": self.token_ceiling,
            "wall_clock_ceiling_s": self.wall_clock_ceiling_s,
            "concurrency": self.concurrency,
            "params": dict(self.params),
        }

    def fingerprint(self) -> str:
        canonical = json.dumps(self.declared(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]
