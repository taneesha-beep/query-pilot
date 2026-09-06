"""Where a call actually goes, and what happens when it is refused.

The walk is simple and the ordering is the point. Candidates come from the registry in
configuration order — the primary endpoint on each of its pools, then each spillover
endpoint on each of its pools — and the first one whose buckets can admit a request now
gets it. **When a bucket is empty the work moves rather than waits**, which is the whole
reason a second Google project was added at 0.4.

Three ways a call can end, and they are deliberately not two:

- it is answered, tokens are charged to the buckets that were only guessing at them, and a
  provider's own remaining-count corrects the guess downwards where it disagrees;
- it is refused by a named quota, the pool shuts for as long as that quota lasts, and the
  walk continues on another pool or another provider;
- it fails terminally, and it is raised. **A terminal failure is not spilled over.** A
  pinned model that has been retired, a key that is refused, a request the provider will
  not parse: these are the same everywhere, and quietly serving the run from a different
  model than the one it declared would corrupt a measurement rather than rescue it.

**Every wall is recorded before it is worked around.** Google's daily allowance for the
models this project can afford is its most valuable unmeasured number, and the run that
first hits that ceiling should be the run that measures it — status, whole body, quota id,
quota value and timestamp — rather than the one that discovers it needs a second run.

What this does *not* do is decide when a run should give up. When every pool for a role is
shut past the wait ceiling it raises :class:`AllPoolsExhausted` carrying when the earliest
one returns. Blocking forever would hang a run against a wall that clears tomorrow, and
spinning would be worse. The budget guard is what turns that exception into a decision.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from query_pilot.client.buckets import BucketBook, ModelBuckets
from query_pilot.client.classify import Action, Disposition, Scope, classify, quota_scope
from query_pilot.client.clock import Clock, SystemClock
from query_pilot.client.config import ClientSettings
from query_pilot.client.errors import ClientError, ProviderHTTPError
from query_pilot.client.registry import Candidate, Registry
from query_pilot.client.resets import UNKNOWN_DAILY_REPROBE_S, seconds_until_daily_reset
from query_pilot.client.retry import RetryPolicy
from query_pilot.client.types import Completion, Message, ModelConfig, ToolSchema


@dataclass(frozen=True, slots=True)
class QuotaWall:
    """One refusal, recorded whole.

    Nothing here is derived or rounded. ``body`` in particular is kept entire: the only
    per-minute refusal this project had was truncated to 300 characters by the probe that
    recorded it, and reconstructing it cost more than storing it would have.
    """

    observed_at: str
    role: str
    provider: str
    pool: str
    model: str
    status: int
    reason: str
    scope: str
    blocked_for_s: float
    quota_id: str | None = None
    quota_metric: str | None = None
    quota_value: int | None = None
    retry_after_s: float | None = None
    body: str = ""

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


QuotaWallSink = Callable[[QuotaWall], None]


class JsonlQuotaWalls:
    """Append every wall to a JSONL file, one row each, created on first use."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def __call__(self, wall: QuotaWall) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write(json.dumps(wall.as_row()) + "\n")


class AllPoolsExhausted(ClientError):
    """Every pool for a role is shut, and the earliest reopens later than we will wait."""

    def __init__(
        self, role: str, *, ready_in_s: float, ready_at_utc: datetime, walls: Sequence[QuotaWall]
    ) -> None:
        named = ", ".join(sorted({f"{w.provider}/{w.model}[{w.pool}]:{w.reason}" for w in walls}))
        super().__init__(
            f"role {role!r}: every pool is shut for another {ready_in_s:.0f}s "
            f"(until {ready_at_utc.isoformat()}); {named or 'no quota was named'}"
        )
        self.role = role
        self.ready_in_s = ready_in_s
        self.ready_at_utc = ready_at_utc
        self.walls = list(walls)


@dataclass
class QuotaScheduler:
    """Spends a role's quota across its pools, and records what refuses it."""

    registry: Registry
    settings: ClientSettings
    clock: Clock = field(default_factory=SystemClock)
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    rng: random.Random = field(default_factory=random.Random)
    on_quota_wall: QuotaWallSink | None = None

    def __post_init__(self) -> None:
        self.book = BucketBook(self.clock)
        self._overall = asyncio.Semaphore(self.settings.max_concurrency)
        self._per_provider: dict[str, asyncio.Semaphore] = {}

    # -- concurrency -------------------------------------------------------------------
    def _provider_slot(self, provider: str) -> asyncio.Semaphore:
        slot = self._per_provider.get(provider)
        if slot is None:
            slot = asyncio.Semaphore(self.settings.per_provider_concurrency)
            self._per_provider[provider] = slot
        return slot

    def buckets(self, candidate: Candidate) -> ModelBuckets:
        return self.book.of(candidate.key, candidate.endpoint.limits)

    # -- the walk ----------------------------------------------------------------------
    async def complete(
        self,
        role: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None,
        config_for: Callable[[Candidate], ModelConfig],
    ) -> Completion:
        candidates = self.registry.candidates(role)
        attempts: dict[str, int] = defaultdict(int)
        set_aside: dict[str, str] = {}
        walls: list[QuotaWall] = []
        last_error: ClientError | None = None
        budget = self.policy.max_attempts * len(candidates) + len(candidates)

        for _ in range(budget):
            live = [c for c in candidates if str(c) not in set_aside]
            if not live:
                raise last_error or AllPoolsExhausted(
                    role,
                    ready_in_s=0.0,
                    ready_at_utc=self.clock.now_utc(),
                    walls=walls,
                )

            ready = [c for c in live if self.buckets(c).ready()]
            if not ready:
                await self._wait_for_one(role, live, walls)
                continue

            candidate = ready[0]
            buckets = self.buckets(candidate)
            buckets.charge_request()
            try:
                async with self._overall, self._provider_slot(candidate.endpoint.provider):
                    completion = await candidate.provider.complete(
                        messages, tools, config_for(candidate)
                    )
            except ClientError as error:
                last_error = error
                decision = classify(error)
                if decision.action is Action.TERMINAL:
                    raise
                if decision.action is Action.BLOCK:
                    walls.append(self._record_wall(role, candidate, error, decision))
                    continue
                attempts[str(candidate)] += 1
                if attempts[str(candidate)] >= self.policy.max_attempts:
                    set_aside[str(candidate)] = decision.reason
                    continue
                await self.clock.sleep(self.policy.delay_s(attempts[str(candidate)], self.rng))
                continue

            buckets.charge_tokens(completion.total_tokens)
            if completion.rate_limit is not None:
                buckets.observe(
                    completion.rate_limit.requests_remaining,
                    completion.rate_limit.tokens_remaining,
                )
            return completion

        raise last_error or AllPoolsExhausted(
            role, ready_in_s=0.0, ready_at_utc=self.clock.now_utc(), walls=walls
        )

    async def _wait_for_one(
        self, role: str, live: Sequence[Candidate], walls: Sequence[QuotaWall]
    ) -> None:
        """Wait for the first pool to reopen, or give up if that is beyond the ceiling."""
        now = self.clock.monotonic()
        earliest = min(self.buckets(candidate).ready_at() for candidate in live)
        wait = earliest - now
        if wait > self.settings.wait_ceiling_s:
            raise AllPoolsExhausted(
                role,
                ready_in_s=wait,
                ready_at_utc=self.clock.now_utc() + timedelta(seconds=wait),
                walls=walls,
            )
        await self.clock.sleep(wait)

    # -- walls -------------------------------------------------------------------------
    def _block_seconds(self, candidate: Candidate, decision: Disposition) -> float:
        if decision.scope is not Scope.DAY:
            return decision.block_for_s or 0.0
        spec = self.registry.config.providers[candidate.endpoint.provider]
        until_reset = seconds_until_daily_reset(spec, self.clock.now_utc())
        return until_reset if until_reset is not None else UNKNOWN_DAILY_REPROBE_S

    def _record_wall(
        self,
        role: str,
        candidate: Candidate,
        error: ClientError,
        decision: Disposition,
    ) -> QuotaWall:
        seconds = self._block_seconds(candidate, decision)
        self.buckets(candidate).block_for(seconds, decision.reason)

        status, body, quota = 0, "", None
        if isinstance(error, ProviderHTTPError):
            status, body, quota = error.status, error.body, error.quota
        scope = decision.scope
        if scope is Scope.UNKNOWN:
            scope = quota_scope(quota, body)
        wall = QuotaWall(
            observed_at=datetime.now(UTC).isoformat(),
            role=role,
            provider=candidate.endpoint.provider,
            pool=candidate.credential.pool,
            model=candidate.endpoint.model,
            status=status,
            reason=decision.reason,
            scope=scope.value,
            blocked_for_s=seconds,
            quota_id=quota.quota_id if quota else None,
            quota_metric=quota.quota_metric if quota else None,
            quota_value=quota.quota_value if quota else None,
            retry_after_s=quota.retry_after_s if quota else None,
            body=body,
        )
        if self.on_quota_wall is not None:
            self.on_quota_wall(wall)
        return wall
