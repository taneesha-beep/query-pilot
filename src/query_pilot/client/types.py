"""The shapes every provider is normalised into.

Nothing in this package knows what the messages are about. It moves text and tool calls
between this project and an LLM provider, and it is written as general infrastructure that
this project happens to use for one purpose.

The two providers this project can afford disagree about more than a base URL, and these
types are where the disagreement is resolved:

- a tool schema goes in ``tools: [{"type": "function", "function": {...}}]`` for the
  OpenAI-shaped API and ``tools: [{"functionDeclarations": [...]}]`` for Google's;
- a tool call comes back at ``choices[0].message.tool_calls`` for one and
  ``candidates[0].content.parts[].functionCall`` for the other;
- and the call's arguments are a **JSON string** in the first and an **object** in the
  second. That difference is a type error waiting to happen, so :class:`ToolCall` holds a
  mapping and each adapter is responsible for getting there.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class Credential:
    """One quota pool's key.

    The convention this project fixed at 0.4: ``<PROVIDER>_API_KEY`` plus optional ``_2``,
    ``_3``, and **each suffixed credential is its own quota pool**, never a fallback key
    for the same one. Google enforces per Cloud project rather than per key, so a second
    key inside one project would share a ceiling; the two configured here were shown
    independent by saturating one and calling the other inside that window.

    ``value`` is excluded from ``repr`` so a credential can be logged, put in an exception,
    or dumped into a ledger row without leaking the key.
    """

    pool: str
    env_var: str
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One request from the model to run a tool.

    ``id`` is the provider's own identifier where it issues one. Google does not, so its
    adapter synthesises a stable id from the call's position: a tool result has to be
    addressed back to a specific call, and OpenAI-shaped APIs address it by id.
    """

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """A tool offered to the model. ``parameters`` is a JSON Schema object."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of a conversation.

    A ``tool`` message carries the result of a call and needs both identifiers: the
    OpenAI-shaped API addresses the result by ``tool_call_id``, and Google's addresses it
    by the function's ``name``. Carrying only one of them makes a conversation that can be
    replayed to one provider and not the other, which would defeat the point of this
    package.
    """

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """What to ask for, including the pinned model string.

    ``extra`` is passed through to the provider verbatim for options one supports and the
    other has no equivalent for — Groq's ``seed``, say. An adapter drops what its provider
    does not accept rather than inventing a translation.
    """

    model: str
    max_output_tokens: int = 512
    temperature: float | None = None
    top_p: float | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Quota facts a provider volunteered in response headers.

    **The header names do not mean what they say**, and Groq's documentation is what fixes
    the meaning: ``x-ratelimit-limit-requests`` is requests per *day* and
    ``x-ratelimit-limit-tokens`` is tokens per *minute*. The fields here are named for what
    the numbers are, not for the headers they came out of. Google serves none of these at
    all, so a Google completion carries ``None``.
    """

    requests_per_day: int | None = None
    requests_remaining: int | None = None
    requests_reset_s: float | None = None
    tokens_per_minute: int | None = None
    tokens_remaining: int | None = None
    tokens_reset_s: float | None = None


@dataclass(frozen=True, slots=True)
class Completion:
    """One answer, in the same shape whichever provider produced it.

    ``model`` is the pinned string that was asked for and ``model_returned`` is what the
    provider says it served. They are kept apart deliberately: Google answers a request for
    ``gemini-3.5-flash-lite`` with a dated build string, and a run ledger that records only
    the request cannot tell that the served model changed underneath it.

    ``raw`` is the decoded response body, untouched, because the ledger records what
    happened rather than this package's reading of it.
    """

    text: str
    tool_calls: tuple[ToolCall, ...]
    prompt_tokens: int | None
    completion_tokens: int | None
    provider: str
    model: str
    pool: str
    latency_s: float
    model_returned: str | None = None
    finish_reason: str | None = None
    rate_limit: RateLimit | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        """Tokens to charge against a bucket. A provider that reports neither costs 0."""
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)
