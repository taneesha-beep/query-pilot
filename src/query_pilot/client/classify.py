"""Retryable against terminal — and the third thing that is neither.

A two-valued classifier gets this wrong, and the 0.4 inventory paid for the evidence:

- **A 429 is not one condition.** The same status covers a limit that clears in two seconds
  and a limit that clears at midnight Pacific. Google's body says which, in ``quotaId``:
  ``GenerateRequestsPerMinutePerProjectPerModel-FreeTier`` against
  ``...PerDayPerProjectPerModel-FreeTier``. A client that backs off identically for both
  sits retrying a request that cannot succeed today.
- **Worse, the daily body asks to be retried.** The recorded refusal on the daily ceiling
  carries ``RetryInfo`` with ``retryDelay: 32s`` beside a ``quotaId`` naming a *per day*
  quota. Honouring that hint gets another 429 thirty-two seconds later, and again, until
  the day ends. **The quota name outranks the retry hint**, and this is the one place in
  the client where a provider's explicit instruction is deliberately disregarded.
- **A 403 must not be read as an auth failure without reading the body.** Groq sits behind
  Cloudflare, which answers Python's default User-Agent with ``error code: 1010`` and a 403
  that looks exactly like a bad key. The 0.4 run reported every Groq model dead because of
  it. That case is neither cleanly retryable nor cleanly terminal: retrying an identical
  request cannot succeed, so it is terminal — but it is **not** an auth failure, it has its
  own name, and its message says what actually fixes it.

So the answer has three actions rather than two. ``RETRY`` is the same pool after a
backoff. ``BLOCK`` shuts one pool for a stated time and sends the work elsewhere — no delay
this classifier computes is ever spent waiting when another pool is free. ``TERMINAL`` is
never retried anywhere.

``reason`` is a short stable label, and it is the field the run ledger will record: the
project's most valuable unmeasured number is Google's daily allowance, and the first run to
walk into that wall should not need a second run to name it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from query_pilot.client.errors import (
    ClientError,
    MalformedResponseError,
    ProviderHTTPError,
    QuotaFact,
    TransportError,
)

#: A per-minute wall clears within a minute by definition. Used only when a provider
#: refuses without naming a delay.
MINUTE_WALL_S = 60.0

_CLOUDFLARE = re.compile(r"error code: 1010|cloudflare", re.IGNORECASE)
_AUTH = re.compile(
    r"invalid[ _-]?api[ _-]?key|unauthorized|invalid authentication|api key not valid",
    re.IGNORECASE,
)
_MISSING_MODEL = re.compile(
    r"no longer available|does not exist|not found|unknown model|decommissioned", re.IGNORECASE
)
_PER_DAY = re.compile(r"perday|requests per day|\bRPD\b|\bTPD\b", re.IGNORECASE)
_PER_MINUTE = re.compile(r"perminute|requests per minute|\bRPM\b|\bTPM\b", re.IGNORECASE)


class Action(Enum):
    RETRY = "retry"
    BLOCK = "block"
    TERMINAL = "terminal"


class Scope(Enum):
    MINUTE = "minute"
    DAY = "day"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Disposition:
    """What to do about one failure, and the label the ledger records."""

    action: Action
    reason: str
    scope: Scope = Scope.UNKNOWN
    #: Seconds to shut the pool for, when the provider named a figure worth honouring. A
    #: daily wall leaves this ``None``: how long that lasts is a question about the
    #: calendar, answered where the calendar is known.
    block_for_s: float | None = None

    @property
    def retryable(self) -> bool:
        return self.action is not Action.TERMINAL


def quota_scope(quota: QuotaFact | None, body: str) -> Scope:
    """Which wall a refusal named. The quota id first; the prose only as a fallback."""
    named = quota.quota_id if quota and quota.quota_id else ""
    if _PER_DAY.search(named):
        return Scope.DAY
    if _PER_MINUTE.search(named):
        return Scope.MINUTE
    if _PER_DAY.search(body):
        return Scope.DAY
    if _PER_MINUTE.search(body):
        return Scope.MINUTE
    return Scope.UNKNOWN


def classify(error: ClientError) -> Disposition:
    """Decide what one failure means. Pure: no clock, no calendar, no I/O."""
    if isinstance(error, TransportError):
        return Disposition(Action.RETRY, "transport")
    if isinstance(error, MalformedResponseError):
        # A success status over something unreadable. Sending the same request again is
        # unlikely to produce a different shape, and an agent must not proceed on a
        # response this package could not read.
        return Disposition(Action.TERMINAL, "malformed_response")
    if not isinstance(error, ProviderHTTPError):
        return Disposition(Action.TERMINAL, "unclassified")

    status, body = error.status, error.body

    if status == 429:
        scope = quota_scope(error.quota, body)
        if scope is Scope.DAY:
            # The retry hint is ignored on purpose: the recorded daily refusal carries a
            # 32-second retryDelay for a ceiling that does not move until tomorrow.
            return Disposition(Action.BLOCK, "quota_day", Scope.DAY, None)
        hint = error.quota.retry_after_s if error.quota else None
        return Disposition(Action.BLOCK, "quota_minute", scope, hint or MINUTE_WALL_S)

    if status == 403:
        if _CLOUDFLARE.search(body):
            return Disposition(Action.TERMINAL, "bot_block")
        if _AUTH.search(body):
            return Disposition(Action.TERMINAL, "auth")
        # Neither, and saying so is better than guessing. Terminal because an identical
        # request cannot be expected to fare differently, and named so that a 403 nobody
        # has seen before shows up in the ledger instead of being absorbed into "auth".
        return Disposition(Action.TERMINAL, "forbidden_unclassified")

    if status == 401:
        return Disposition(Action.TERMINAL, "auth")
    if status == 402:
        # Cerebras answers every model with this. A free tier that has been withdrawn is
        # not a transient condition.
        return Disposition(Action.TERMINAL, "payment_required")
    if status == 404 or _MISSING_MODEL.search(body):
        return Disposition(Action.TERMINAL, "model_not_found")
    if status == 400:
        return Disposition(Action.TERMINAL, "bad_request")
    if status in (408, 409) or status >= 500:
        # 503 belongs here on evidence: gemini-3.8-flash returned one mid-probe at 0.4 and
        # answered on the next call.
        return Disposition(Action.RETRY, "server_error")
    return Disposition(Action.TERMINAL, f"http_{status}")
