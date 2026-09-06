"""An async client over several LLM providers with several quota pools each.

General infrastructure. **Nothing in this package knows what the prompts are about**, and
nothing in it should learn: it moves messages and tool calls, and this project happens to
use it for one purpose.

What the two providers within reach of a free tier actually disagree about — request shape,
tool schema wrapping, where a tool call comes back, whether its arguments are a string or
an object, which quota a 429 names — is normalised in `providers/`. What they disagree
about that cannot be normalised, such as Google serving no rate-limit headers at all, is
carried as an absence rather than filled in with a guess.
"""

from query_pilot.client.client import Client
from query_pilot.client.config import ClientConfig, Endpoint, Limits, RoleSpec, discover_pools
from query_pilot.client.errors import (
    ClientError,
    ConfigError,
    MalformedResponseError,
    ProviderHTTPError,
    QuotaFact,
    TransportError,
)
from query_pilot.client.http import USER_AGENT, HttpClient, HttpResponse, HttpxClient
from query_pilot.client.providers import Provider
from query_pilot.client.registry import Candidate, Registry
from query_pilot.client.types import (
    Completion,
    Credential,
    Message,
    ModelConfig,
    RateLimit,
    ToolCall,
    ToolSchema,
)

__all__ = [
    "USER_AGENT",
    "Candidate",
    "Client",
    "ClientConfig",
    "ClientError",
    "Completion",
    "ConfigError",
    "Credential",
    "Endpoint",
    "HttpClient",
    "HttpResponse",
    "HttpxClient",
    "Limits",
    "MalformedResponseError",
    "Message",
    "ModelConfig",
    "Provider",
    "ProviderHTTPError",
    "QuotaFact",
    "RateLimit",
    "Registry",
    "RoleSpec",
    "ToolCall",
    "ToolSchema",
    "TransportError",
    "discover_pools",
]
