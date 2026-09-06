"""The protocol every adapter satisfies, and the parsing both of them need.

An adapter instance is bound to **one credential pool**, because a pool is a quota pool and
quota control keys its buckets on the pool. The signature stays the one the design calls
for — ``complete(messages, tools, model_config)`` — with the model string arriving in the
config rather than being written at a call site.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from query_pilot.client.errors import ConfigError
from query_pilot.client.http import HttpClient
from query_pilot.client.types import Completion, Credential, Message, ModelConfig, ToolSchema

# "2h31m12s", "14.025s", "1h46m33.599s" — Groq's reset countdowns, and the retryDelay
# Google puts in a RetryInfo detail ("32s").
_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?\s*$")


def parse_duration_s(text: str | None) -> float | None:
    """Seconds from a provider's duration string, or ``None`` if it is not one.

    A bare number is read as seconds, which is what a ``retry-after`` header carries.
    """
    if text is None:
        return None
    raw = text.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    match = _DURATION.fullmatch(raw)
    if match is None or not any(match.groups()):
        return None
    hours, minutes, seconds = (float(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


class Provider(Protocol):
    """One provider, one credential pool, any of that provider's models."""

    name: str
    pool: str

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None,
        model_config: ModelConfig,
    ) -> Completion: ...


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """A provider as the configuration file describes it."""

    name: str
    style: str
    url: str
    key_env: str


def build_provider(spec: ProviderSpec, credential: Credential, http: HttpClient) -> Provider:
    """Make the adapter for a provider's wire style.

    Imported here rather than at module scope so the two adapters can import this module
    for :func:`parse_duration_s` without a cycle.
    """
    from query_pilot.client.providers.google import GoogleProvider
    from query_pilot.client.providers.openai_compat import OpenAICompatibleProvider

    if spec.style == "openai":
        return OpenAICompatibleProvider(spec, credential, http)
    if spec.style == "google":
        return GoogleProvider(spec, credential, http)
    raise ConfigError(f"provider {spec.name!r}: unknown wire style {spec.style!r}")


def generation_options(model_config: ModelConfig) -> dict[str, Any]:
    """The options a caller set, dropping the ones left unset. Provider-neutral names."""
    options: dict[str, Any] = {}
    if model_config.temperature is not None:
        options["temperature"] = model_config.temperature
    if model_config.top_p is not None:
        options["top_p"] = model_config.top_p
    return options


def lowercased(headers: Mapping[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}
