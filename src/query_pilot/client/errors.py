"""What went wrong, in enough detail to decide what to do about it later.

Nothing here decides whether a failure is worth retrying. These types carry **facts** —
the status, the untruncated body, and whatever quota the provider named inside it — and
the classifier that turns facts into a decision arrives with quota control.

That split is deliberate. A 403 from Groq can be an expired key or Cloudflare refusing
Python's default User-Agent, and the two are indistinguishable until something reads the
body; a 429 can be a limit that clears in two seconds or one that clears at midnight
Pacific, and only the body says which. An exception type that decided at the throw site
would have to guess.

**Request headers are never recorded anywhere in this module.** The API key travels in
one, and an exception is the most likely thing in a client to be logged verbatim.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime


class ClientError(Exception):
    """Base for everything this package raises."""


class ConfigError(ClientError):
    """The configuration or the environment cannot produce a working call."""


@dataclass(frozen=True, slots=True)
class QuotaFact:
    """The ceiling a provider says it enforced, as it named it.

    Google's 429 body carries a ``QuotaFailure`` detail whose ``quotaId`` distinguishes
    ``GenerateRequestsPerMinutePerProjectPerModel-FreeTier`` from
    ``...PerDayPerProjectPerModel-FreeTier``. Groq names its ceiling in prose:
    ``on requests per minute (RPM): Limit 30``. Both reach this shape.

    ``quota_id`` is the provider's own string, never a normalised one. It is the evidence,
    and Google's daily allowance for the models this project can afford is still unmeasured
    — the first run to hit that wall fills it in from exactly this field.
    """

    quota_id: str | None = None
    quota_metric: str | None = None
    quota_value: int | None = None
    retry_after_s: float | None = None
    message: str = ""


class ProviderHTTPError(ClientError):
    """A provider answered, and not with success."""

    def __init__(
        self,
        *,
        status: int,
        body: str,
        provider: str,
        model: str,
        pool: str,
        headers: Mapping[str, str] | None = None,
        quota: QuotaFact | None = None,
    ) -> None:
        summary = " ".join(body.split())[:200]
        super().__init__(f"{provider}/{model} [{pool}] HTTP {status}: {summary}")
        self.status = status
        # Kept whole. Truncating it here is how the quota a provider named gets lost.
        self.body = body
        self.provider = provider
        self.model = model
        self.pool = pool
        self.headers: Mapping[str, str] = dict(headers or {})
        self.quota = quota
        self.observed_at = datetime.now(UTC).isoformat()


class TransportError(ClientError):
    """No answer: a timeout, a reset connection, a DNS failure."""

    def __init__(self, message: str, *, provider: str, model: str, pool: str) -> None:
        super().__init__(f"{provider}/{model} [{pool}] {message}")
        self.provider = provider
        self.model = model
        self.pool = pool
        self.observed_at = datetime.now(UTC).isoformat()


class MalformedResponseError(ClientError):
    """A success status over something this package cannot read.

    Its own class rather than a ``ValueError`` because it is a live possibility, not a
    programming mistake: a truncated body, a tool call whose arguments are not the JSON
    the provider's schema promises, a response with no candidate in it at all.
    """

    def __init__(
        self, message: str, *, provider: str, model: str, pool: str, body: str = ""
    ) -> None:
        super().__init__(f"{provider}/{model} [{pool}] {message}")
        self.provider = provider
        self.model = model
        self.pool = pool
        self.body = body
