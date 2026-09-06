"""The call a caller makes.

``complete`` takes a **role**, never a model string. That is not a convenience: it is the
one structural reason a retired model cannot become a search through call sites, because
there is no call site that names a model to search for.

Where the call goes, what happens when a quota refuses it, and what is recorded when one
does are all decided by the scheduler. This class is the seam: a caller asks for ``cheap``
and gets an answer, or an exception that says which wall stopped it and when that wall
lifts.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from query_pilot.client.clock import Clock, SystemClock
from query_pilot.client.config import ClientConfig
from query_pilot.client.http import HttpClient, HttpxClient
from query_pilot.client.registry import Candidate, Registry
from query_pilot.client.retry import RetryPolicy
from query_pilot.client.scheduler import JsonlQuotaWalls, QuotaScheduler, QuotaWallSink
from query_pilot.client.types import Completion, Message, ModelConfig, ToolSchema

DEFAULT_MAX_OUTPUT_TOKENS = 512

#: Where a refusal that names a quota is recorded when a caller does not say otherwise.
#: Gitignored, and cheap: Google's daily allowance is this project's most valuable
#: unmeasured number, and the first run to walk into that wall should be the one that
#: measures it rather than the one that finds out a second run is needed.
DEFAULT_QUOTA_WALL_PATH = Path("runs/quota-walls.jsonl")


class Client:
    """One entry point over every provider, pool and pinned model."""

    def __init__(
        self,
        registry: Registry,
        *,
        owns_http: HttpClient | None = None,
        clock: Clock | None = None,
        policy: RetryPolicy | None = None,
        rng: random.Random | None = None,
        quota_walls: QuotaWallSink | None = None,
    ) -> None:
        self.registry = registry
        self._owns_http = owns_http
        self.scheduler = QuotaScheduler(
            registry=registry,
            settings=registry.config.settings,
            clock=clock or SystemClock(),
            policy=policy or RetryPolicy(),
            rng=rng or random.Random(),
            on_quota_wall=quota_walls,
        )

    @classmethod
    def from_config(
        cls,
        path: Path | str | None = None,
        *,
        http: HttpClient | None = None,
        environ: Mapping[str, str] | None = None,
        quota_walls: QuotaWallSink | Path | str | None = DEFAULT_QUOTA_WALL_PATH,
        clock: Clock | None = None,
        policy: RetryPolicy | None = None,
        rng: random.Random | None = None,
    ) -> Client:
        """Build from the committed configuration file.

        A caller that passes its own ``http`` keeps ownership of it; one that does not gets
        an httpx-backed client that this object closes. ``quota_walls`` takes a path, a
        callable, or ``None`` to record nothing.
        """
        owned = None if http is not None else HttpxClient()
        if isinstance(quota_walls, str | Path):
            quota_walls = JsonlQuotaWalls(quota_walls)
        return cls(
            Registry(ClientConfig.load(path), http or owned, environ),
            owns_http=owned,
            clock=clock,
            policy=policy,
            rng=rng,
            quota_walls=quota_walls,
        )

    def model_config(
        self,
        candidate: Candidate,
        *,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float | None = None,
        top_p: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> ModelConfig:
        return ModelConfig(
            model=candidate.endpoint.model,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            top_p=top_p,
            extra=dict(extra or {}),
        )

    async def complete(
        self,
        role: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None = None,
        *,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float | None = None,
        top_p: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> Completion:
        return await self.scheduler.complete(
            role,
            messages,
            tools,
            lambda candidate: self.model_config(
                candidate,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                top_p=top_p,
                extra=extra,
            ),
        )

    async def aclose(self) -> None:
        if self._owns_http is not None:
            await self._owns_http.aclose()
