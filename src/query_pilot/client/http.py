"""The only module in this package that performs network I/O.

Adapters take an :class:`HttpClient` rather than reaching for a library, which is what lets
the test suite exercise every request shape and every error path without a socket or a key
— the rule that keeps this project's tests runnable on a day when its quota is gone.

**A real User-Agent is not decoration.** Groq sits behind Cloudflare, which answers
Python's default agent with ``error code: 1010`` and HTTP 403. The 0.4 inventory reported
every Groq model dead for that reason before anyone read the body. The header below is why
this package does not rediscover it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from query_pilot.client.errors import TransportError

USER_AGENT = "query-pilot/0.1 (+https://github.com/taneesha-beep/query-pilot)"

# Separate connect and read budgets. A provider that never answers and a provider that
# never accepts a connection are the same failure to a single timeout number, and the
# second is worth spilling over from sooner than the first.
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 90.0


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A response reduced to what this package needs. Header names are lowercased."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    latency_s: float = 0.0

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class HttpClient(Protocol):
    """Post JSON, get a response back, or raise :class:`TransportError`.

    Implementations do not raise for a non-2xx status: a 429 body names the quota that
    refused the call and a 403 body says whether it was a key or a bot block, so every
    status reaches the adapter with its body intact.
    """

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any],
        provider: str,
        model: str,
        pool: str,
    ) -> HttpResponse: ...

    async def aclose(self) -> None: ...


class HttpxClient:
    """:class:`HttpClient` over ``httpx``, with one connection pool for the process."""

    def __init__(
        self,
        *,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        read_timeout_s: float = READ_TIMEOUT_S,
        user_agent: str = USER_AGENT,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s),
            headers={"User-Agent": user_agent},
        )

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any],
        provider: str,
        model: str,
        pool: str,
    ) -> HttpResponse:
        try:
            response = await self._client.post(url, headers=dict(headers), json=dict(json))
        except httpx.TimeoutException as exc:
            raise TransportError(
                f"timeout: {type(exc).__name__}", provider=provider, model=model, pool=pool
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportError(
                f"transport: {type(exc).__name__}: {exc}",
                provider=provider,
                model=model,
                pool=pool,
            ) from exc
        return HttpResponse(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.content,
            latency_s=response.elapsed.total_seconds(),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
