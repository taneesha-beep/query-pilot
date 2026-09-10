"""Stop a run at its declared ceiling rather than letting it degrade into a partial result.

This is the difference between *reporting* cost and *enforcing* a budget, and the gap
Phase 1 exists to close. A run that quietly stops short and then produces a summary
shaped like a finished one is worse than a run that fails, because the number gets copied.

**Five ways a run can be incomplete, kept apart on purpose.** They mean different things
to whoever reads the result and collapsing them into one flag throws that away: a run that
hit its token ceiling was too expensive, a run that hit its wall clock was stuck, a run
that exhausted every pool ran out of somebody else's quota, a run that hit a fatal client
error was misconfigured, and a run an operator stopped was a decision. A sixth,
``killed``, is never written by anything here — it is what a reader infers when a segment
has no ``run_end`` at all, because a process that has been ``SIGKILL``ed cannot write one.

**The two ceilings are not symmetric, and this is the most consequential decision in the
item.** The token ceiling is *inherited*: a resumed run's remaining budget is the declared
ceiling minus everything already recorded against that run ID, across every session.
Refreshing it per session would mean a run spanning three days spends three times its
declared ceiling, which is the ceiling silently ceasing to be one. The wall-clock ceiling
is *per session*: a run that must span a quota reset is the normal case here, not the
failure case, and a ceiling counting the hours a run was not running would abort it for
waiting. **Tokens are a stock; time is a rate.** The consequence, stated rather than
hidden: a run whose ceiling was set too low cannot finish until the ceiling is raised, and
raising it changes the run's fingerprint, so it is a deliberate recorded act rather than
a quiet one.

**Do failed attempts count? Yes — every token a provider reports, whatever the outcome.**
A refused request reports none and costs nothing. A malformed 200 and a generation that
answered wrongly both report tokens and both count. The guard enforces spend, and Phase
5's cost per solved task pays for failed attempts too.

**Ceilings are checked before a task is dispatched, never in the middle of one.** When one
trips the loop stops dispatching and waits for what is already running, recording each.
Abandoning work mid-flight is how a ledger gets a half-row; waiting is how a ceiling gets
overshot; waiting is the better trade because the overshoot is bounded and can be stated —
**at most ``concurrency - 1`` tasks beyond the one that crossed the line**, because a token
count does not exist until the answer arrives. The bound is in tasks rather than tokens
because tokens per task is not measured until 2.4 and will not be guessed at here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from query_pilot.client.classify import classify
from query_pilot.client.clock import Clock, SystemClock
from query_pilot.client.errors import ClientError
from query_pilot.client.scheduler import AllPoolsExhausted


class IncompleteReason(StrEnum):
    """Why a run stopped short. Never one flag: these do not mean the same thing."""

    TOKEN_CEILING = "token_ceiling"
    WALL_CLOCK_CEILING = "wall_clock_ceiling"
    POOLS_EXHAUSTED = "pools_exhausted"
    CLIENT_FATAL = "client_fatal"
    OPERATOR = "operator"
    #: Inferred by a reader from a missing ``run_end``, never written by the loop: a
    #: process killed outright does not get to record its own death.
    KILLED = "killed"


#: Terminal client failures that describe a broken configuration rather than a bad task.
#: Every remaining task would fail identically against a retired model or a refused key,
#: so the run stops instead of burning the list proving it. `bad_request` and
#: `malformed_response` are deliberately absent: those can be about one task's content.
#:
#: `config` is here because `ConfigError`'s own docstring — "the configuration or the
#: environment cannot produce a working call" — is this set's definition written out. A run
#: with no credential for its role failed ten identical tasks proving that once; at 150 it
#: would prove it fifteen times over.
FATAL_ERROR_CLASSES = frozenset(
    {"auth", "bot_block", "config", "payment_required", "model_not_found"}
)


def fatal_reason(error: BaseException) -> IncompleteReason | None:
    """Whether a failure that ended a task should also end the run."""
    if isinstance(error, AllPoolsExhausted):
        return IncompleteReason.POOLS_EXHAUSTED
    if isinstance(error, ClientError) and classify(error).reason in FATAL_ERROR_CLASSES:
        return IncompleteReason.CLIENT_FATAL
    return None


@dataclass
class BudgetGuard:
    """What a run is allowed to spend, and whether it has stopped.

    ``tokens_spent`` opens at whatever the ledger already recorded for this run ID, which
    is what makes the token ceiling bind across resumes rather than per session.
    """

    token_ceiling: int
    wall_clock_ceiling_s: float
    clock: Clock = field(default_factory=SystemClock)
    tokens_spent: int = 0
    stopped: IncompleteReason | None = None
    _started_at: float | None = None

    def start_session(self) -> None:
        """Mark the beginning of *this* session, which is what the wall clock measures."""
        self._started_at = self.clock.monotonic()

    @property
    def session_elapsed_s(self) -> float:
        if self._started_at is None:
            return 0.0
        return self.clock.monotonic() - self._started_at

    @property
    def tokens_remaining(self) -> int:
        return max(0, self.token_ceiling - self.tokens_spent)

    def spend(self, tokens: int) -> None:
        """Charge tokens. Called for every attempt, answered or refused."""
        self.tokens_spent += tokens

    def check(self) -> IncompleteReason | None:
        """Whether another task may be dispatched. Tokens first: they bind first.

        docs/PROVIDERS.md's finding is that the token budget binds long before wall clock
        does, so when both have been crossed the token ceiling is the more informative
        thing to report.
        """
        if self.stopped is not None:
            return self.stopped
        if self.tokens_spent >= self.token_ceiling:
            return IncompleteReason.TOKEN_CEILING
        if self.session_elapsed_s >= self.wall_clock_ceiling_s:
            return IncompleteReason.WALL_CLOCK_CEILING
        return None

    def stop(self, reason: IncompleteReason) -> None:
        """Record the first reason the run stopped. Later ones do not overwrite it."""
        if self.stopped is None:
            self.stopped = reason


class RequestCeiling:
    """A client wrapper that counts requests and stops the **run** at a ceiling.

    **A unit no run declaration carries, and deliberately so.** A run declares tokens and
    wall clock because those are what a budget is spent in. Requests are what a free tier
    *refuses* in, and for an agent whose cost is not one request a task — A1 makes anywhere
    between one and ``TURN_LIMIT + REPAIR_LIMIT`` of them — a request count is the only
    bound whose worst case can be stated before the run starts. Putting it in the run config
    would make it part of the fingerprint and therefore inherited across resumes, which is
    the opposite of what it is for: this is a **per-session** backstop, reset every time,
    and it is how a long run is spent in reviewable stages.

    **Wrapping rather than extending, and stopping the run rather than raising, are both
    deliberate.** Raising would fail one task and let the next one start, which is the
    opposite of a ceiling. Calling :meth:`BudgetGuard.stop` puts the run down the path it
    already has for a person deciding to stop it: the agent sees it at the next turn
    boundary, tasks that never started are recorded unrun rather than failed, and a resume
    picks them up exactly where this session left off.

    It counts every request it is asked to make, including the one that reaches the ceiling
    — that request is still sent, because refusing it would leave a turn recorded in the
    transcript with no answer beside it in the ledger.
    """

    def __init__(
        self,
        client: object,
        guard: BudgetGuard,
        ceiling: int,
        *,
        on_ceiling: Callable[[int], None] | None = None,
    ) -> None:
        self.client = client
        self.guard = guard
        self.ceiling = ceiling
        self.requests = 0
        # Saying so is a script's job, not a library's. The run report records it either way.
        self._on_ceiling = on_ceiling

    async def complete(self, *args: object, **kwargs: object) -> object:
        self.requests += 1
        if self.requests >= self.ceiling:
            if self.requests == self.ceiling and self._on_ceiling is not None:
                self._on_ceiling(self.ceiling)
            self.guard.stop(IncompleteReason.OPERATOR)
        return await self.client.complete(*args, **kwargs)  # type: ignore[attr-defined]
