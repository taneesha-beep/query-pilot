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

from query_pilot.client.buckets import BucketBook, ModelBuckets, TokenBucket
from query_pilot.client.classify import Action, Disposition, Scope, classify
from query_pilot.client.client import Client
from query_pilot.client.clock import Clock, SystemClock
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
from query_pilot.client.resets import seconds_until_daily_reset
from query_pilot.client.retry import RetryPolicy
from query_pilot.client.scheduler import (
    AllPoolsExhausted,
    JsonlQuotaWalls,
    QuotaScheduler,
    QuotaWall,
)
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
    "Action",
    "AllPoolsExhausted",
    "BucketBook",
    "Candidate",
    "Client",
    "ClientConfig",
    "ClientError",
    "Clock",
    "Completion",
    "ConfigError",
    "Credential",
    "Disposition",
    "Endpoint",
    "HttpClient",
    "HttpResponse",
    "HttpxClient",
    "JsonlQuotaWalls",
    "Limits",
    "MalformedResponseError",
    "Message",
    "ModelBuckets",
    "ModelConfig",
    "Provider",
    "ProviderHTTPError",
    "QuotaFact",
    "QuotaScheduler",
    "QuotaWall",
    "RateLimit",
    "Registry",
    "RetryPolicy",
    "RoleSpec",
    "Scope",
    "SystemClock",
    "TokenBucket",
    "ToolCall",
    "ToolSchema",
    "TransportError",
    "classify",
    "discover_pools",
    "seconds_until_daily_reset",
]
