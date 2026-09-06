"""The call a caller makes.

``complete`` takes a **role**, never a model string. That is not a convenience: it is the
one structural reason a retired model cannot become a search through call sites, because
there is no call site that names a model to search for.

This is the 1.1 shape. It resolves a role to its candidates and takes the first. Quota
control replaces that single line with a walk governed by buckets, spillover and classified
retry; nothing above this method changes when it does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from query_pilot.client.config import ClientConfig
from query_pilot.client.http import HttpClient, HttpxClient
from query_pilot.client.registry import Candidate, Registry
from query_pilot.client.types import Completion, Message, ModelConfig, ToolSchema

DEFAULT_MAX_OUTPUT_TOKENS = 512


class Client:
    """One entry point over every provider, pool and pinned model."""

    def __init__(self, registry: Registry, *, owns_http: HttpClient | None = None) -> None:
        self.registry = registry
        self._owns_http = owns_http

    @classmethod
    def from_config(
        cls,
        path: Path | str | None = None,
        *,
        http: HttpClient | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Client:
        """Build from the committed configuration file.

        A caller that passes its own ``http`` keeps ownership of it; one that does not gets
        an httpx-backed client that this object closes.
        """
        owned = None if http is not None else HttpxClient()
        return cls(
            Registry(ClientConfig.load(path), http or owned, environ),
            owns_http=owned,
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
        candidate = self.registry.candidates(role)[0]
        return await candidate.provider.complete(
            messages,
            tools,
            self.model_config(
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
