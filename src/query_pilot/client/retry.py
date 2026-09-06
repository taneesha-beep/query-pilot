"""Exponential backoff with jitter, as a schedule that can be inspected.

The delay for an attempt is a pure function of the attempt number and a random draw, which
is what makes the schedule testable without spending it: a test asserts that the sequence
doubles, that it is capped, and that each delay sits inside its jitter band, and it does
that in no time at all.

Jitter is not decoration. Several coroutines refused by the same per-minute ceiling in the
same instant will otherwise return in the same instant, and a synchronised retry is how a
client turns one refusal into a burst of them.

This governs the failures a provider gave no guidance about — a 503, a timeout, a reset
connection. A refusal that *names* a quota is not backed off against at all: it shuts that
pool and the work goes to another one.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How long to wait before trying the same pool again, and how often to bother.

    Four attempts at 0.5 s doubling is about three and a half seconds of waiting before a
    candidate is set aside and the work moves on. Against providers whose warm latency was
    measured at 0.5-0.9 s, waiting longer than that on one pool while another sits idle is
    just slower.
    """

    max_attempts: int = 4
    base_s: float = 0.5
    factor: float = 2.0
    max_delay_s: float = 30.0
    jitter: float = 0.25

    def nominal_s(self, attempt: int) -> float:
        """The un-jittered delay after ``attempt`` failures. Attempts count from 1."""
        if attempt < 1:
            raise ValueError("attempts count from 1")
        return min(self.base_s * self.factor ** (attempt - 1), self.max_delay_s)

    def delay_s(self, attempt: int, rng: random.Random) -> float:
        """The delay actually waited: the nominal one, jittered either side."""
        nominal = self.nominal_s(attempt)
        return nominal * rng.uniform(1.0 - self.jitter, 1.0 + self.jitter)
