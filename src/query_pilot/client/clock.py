"""Time, taken as an argument.

Every wait in this package goes through a :class:`Clock`, which is what lets the test suite
assert on a backoff *schedule* — the sequence of delays and what each was computed from —
without spending that many seconds. A test that sleeps through its own backoff is slow, and
a slow test that also depends on timing is flaky, and a flaky test proving retry works is
worse than no test at all.

Three clock readings are needed and they are not interchangeable. ``monotonic`` measures
intervals and cannot go backwards. ``now_utc`` answers what day it is, which a daily quota
needs and a monotonic reading cannot give. ``sleep`` is what a stub replaces.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def now_utc(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real one."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)
