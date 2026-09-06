"""One adapter per provider, behind one protocol."""

from query_pilot.client.providers.base import Provider, ProviderSpec, build_provider
from query_pilot.client.providers.google import GoogleProvider
from query_pilot.client.providers.openai_compat import OpenAICompatibleProvider

__all__ = [
    "GoogleProvider",
    "OpenAICompatibleProvider",
    "Provider",
    "ProviderSpec",
    "build_provider",
]
