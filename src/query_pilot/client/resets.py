"""When a daily ceiling comes back.

Two providers, two different answers, and the difference was measured rather than assumed.

**Groq refills continuously.** Its requests-per-day reset countdown advanced 86.4 seconds
per request — one thousandth of a day — which is a 1,000-per-day bucket topping itself up
rather than resetting on a boundary. So Groq has no reset time configured, and its daily
ceiling is modelled by the bucket alone.

**Google resets on a boundary**, at midnight Pacific, which is Google's documented figure
and not one this project has watched happen. It is configured rather than hardcoded, and
labelled in the configuration file as declared rather than observed.

**When the boundary is unknown, the client waits an hour and asks again.** That is a
re-probe cadence, not a claim about anyone's quota: the wall is real, its end is not known,
and an hour is short enough that a ceiling clearing sooner is not wasted and long enough
that asking is not spam. Writing the pool off for a day would throw away capacity that may
have returned; guessing a boundary would invent a measurement.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from query_pilot.client.errors import ConfigError
from query_pilot.client.providers.base import ProviderSpec

UNKNOWN_DAILY_REPROBE_S = 3600.0


def seconds_until_daily_reset(spec: ProviderSpec, now_utc: datetime) -> float | None:
    """Seconds until this provider's daily quotas roll over, or ``None`` if unconfigured."""
    if not spec.daily_reset_time or not spec.daily_reset_timezone:
        return None
    try:
        zone = ZoneInfo(spec.daily_reset_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(
            f"provider {spec.name!r}: unknown daily_reset_timezone {spec.daily_reset_timezone!r}"
        ) from exc
    try:
        hour, minute = (int(part) for part in spec.daily_reset_time.split(":", 1))
    except ValueError as exc:
        raise ConfigError(
            f"provider {spec.name!r}: daily_reset_time must be HH:MM, got {spec.daily_reset_time!r}"
        ) from exc

    local = now_utc.astimezone(zone)
    boundary = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if boundary <= local:
        boundary += timedelta(days=1)
    return (boundary - local).total_seconds()
