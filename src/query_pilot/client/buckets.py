"""Token buckets, one per pool per model per dimension.

**Not one per provider.** The measurements say so plainly: Groq serves requests-per-day and
tokens-per-minute per model per organization — two of its models showed different remaining
counters after identical bursts — and Google serves requests-per-minute per model per
project, where a project is what this package calls a pool. A bucket keyed on the provider
alone is wrong in both directions at once. It blocks a model that has quota left because a
sibling model spent its own, and it lets through a call that is certain to be refused.

**Which dimensions exist is decided by what was measured, not by what a plan named.** Groq
gets requests and tokens per minute and per day; Google's spillover model gets
requests-per-minute and nothing else, because nothing else has ever been observed and
Google publishes no per-model free-tier figures. A limit left out of the configuration is
**unmodelled, not unlimited** — the client cannot pre-empt a wall it has no number for. It
can only walk into it, read what the provider named, and record that.

**Requests are charged on admission and tokens on completion**, because a token count does
not exist until the response does. So a token ceiling can be overshot by whatever is in
flight, which is what the concurrency caps are really bounding.
"""

from __future__ import annotations

from dataclasses import dataclass

from query_pilot.client.clock import Clock
from query_pilot.client.config import Limits

MINUTE_S = 60.0
DAY_S = 86_400.0


class TokenBucket:
    """A ceiling that refills continuously.

    Continuous refill is what Groq was observed doing: its requests-per-day reset countdown
    advanced 86.4 seconds per request, exactly one thousandth of a day, which is a
    1,000-per-day bucket topping itself up rather than resetting on a boundary.

    For a ceiling that really does reset on a boundary — Google's daily quotas — continuous
    refill is optimistic, and deliberately so. The bucket is the guess; the provider's own
    429 is the authority, and a wall it names closes the pool outright until the boundary
    passes. Optimism that is corrected by evidence costs one refused request. Pessimism
    costs a day of capacity that was there all along.
    """

    def __init__(self, capacity: float, refill_per_s: float, clock: Clock) -> None:
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self._clock = clock
        self._tokens = float(capacity)
        self._at = clock.monotonic()

    def _advance(self) -> None:
        now = self._clock.monotonic()
        elapsed = now - self._at
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_s)
            self._at = now

    @property
    def available(self) -> float:
        self._advance()
        return self._tokens

    def ready_at(self, cost: float = 1.0) -> float:
        """The monotonic time at which ``cost`` can be taken. In the past means now."""
        self._advance()
        shortfall = cost - self._tokens
        if shortfall <= 0:
            return self._at
        if self.refill_per_s <= 0:
            return float("inf")
        return self._at + shortfall / self.refill_per_s

    def take(self, cost: float = 1.0) -> None:
        """Charge unconditionally, even into deficit.

        A response that cost more tokens than were left is a fact, not a request for
        permission. The deficit is carried and the bucket simply stays shut for longer.
        """
        self._advance()
        self._tokens -= cost


@dataclass
class ModelBuckets:
    """Every ceiling that applies to one model on one pool, plus any wall it has hit.

    ``blocked_until`` is what a provider's own refusal sets, and it outranks every bucket
    here. A bucket says what this package believes; a 429 says what the provider enforced.
    """

    limits: Limits
    clock: Clock
    requests_per_minute: TokenBucket | None = None
    requests_per_day: TokenBucket | None = None
    tokens_per_minute: TokenBucket | None = None
    tokens_per_day: TokenBucket | None = None
    blocked_until: float | None = None
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        def bucket(limit: int | None, period_s: float) -> TokenBucket | None:
            return None if limit is None else TokenBucket(limit, limit / period_s, self.clock)

        self.requests_per_minute = bucket(self.limits.rpm, MINUTE_S)
        self.requests_per_day = bucket(self.limits.rpd, DAY_S)
        self.tokens_per_minute = bucket(self.limits.tpm, MINUTE_S)
        self.tokens_per_day = bucket(self.limits.tpd, DAY_S)

    @property
    def _all(self) -> list[TokenBucket]:
        return [
            b
            for b in (
                self.requests_per_minute,
                self.requests_per_day,
                self.tokens_per_minute,
                self.tokens_per_day,
            )
            if b is not None
        ]

    def ready_at(self) -> float:
        """When one more request may go out: the latest of every ceiling and any wall."""
        now = self.clock.monotonic()
        times = [b.ready_at(1.0) for b in (self.requests_per_minute, self.requests_per_day) if b]
        # A token bucket in deficit holds the next request back too — sending one when the
        # per-minute token ceiling is spent earns a 429 rather than an answer.
        times += [b.ready_at(0.0) for b in (self.tokens_per_minute, self.tokens_per_day) if b]
        if self.blocked_until is not None:
            times.append(self.blocked_until)
        return max([now, *times]) if times else now

    def ready(self) -> bool:
        """Whether a request may go out now.

        Asked as a question rather than compared against a caller's earlier clock reading:
        `ready_at` advances the buckets as it goes, so it necessarily reports a time at or
        after the moment it was called, and comparing that against a reading taken a moment
        earlier makes everything look permanently unready.
        """
        return self.ready_at() <= self.clock.monotonic()

    def charge_request(self) -> None:
        for bucket in (self.requests_per_minute, self.requests_per_day):
            if bucket is not None:
                bucket.take(1.0)

    def charge_tokens(self, tokens: int) -> None:
        if tokens <= 0:
            return
        for bucket in (self.tokens_per_minute, self.tokens_per_day):
            if bucket is not None:
                bucket.take(float(tokens))

    def block_for(self, seconds: float, reason: str) -> float:
        """Shut this pool and model until a wall clears. Returns when that is."""
        until = self.clock.monotonic() + max(0.0, seconds)
        if self.blocked_until is None or until > self.blocked_until:
            self.blocked_until, self.blocked_reason = until, reason
        return self.blocked_until

    def observe(self, requests_remaining: int | None, tokens_remaining: int | None) -> None:
        """Take a provider's own count when it is less generous than this one.

        Groq reports what it has left in every response. Where its figure is lower than the
        bucket's, the bucket is wrong — a previous run spent quota this process never saw —
        and it is corrected downwards. Never upwards: a provider that says there is more
        room than this client thinks is not a reason to spend past a ceiling it modelled.
        """
        for bucket, remaining in (
            (self.requests_per_day, requests_remaining),
            (self.tokens_per_minute, tokens_remaining),
        ):
            if bucket is None or remaining is None:
                continue
            if remaining < bucket.available:
                bucket.take(bucket.available - remaining)


class BucketBook:
    """Every model's buckets, keyed on provider, pool and model together."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._books: dict[tuple[str, str, str], ModelBuckets] = {}

    def of(self, key: tuple[str, str, str], limits: Limits) -> ModelBuckets:
        book = self._books.get(key)
        if book is None:
            book = ModelBuckets(limits=limits, clock=self._clock)
            self._books[key] = book
        return book

    def known(self) -> dict[tuple[str, str, str], ModelBuckets]:
        return dict(self._books)
