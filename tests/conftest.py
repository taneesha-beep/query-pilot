"""Stubs and recorded provider bodies shared by the client tests.

**No test in this project makes a live API call.** The client takes its HTTP layer as an
argument for exactly that reason: every request shape and every error path below is
exercised against bodies a provider actually returned on 2026-09-06, recorded in
`docs/provider-check.json` and lifted into `tests/provider_bodies/`, with no socket and no
key. That directory's README says which bodies are verbatim and which one is not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from query_pilot.client.http import HttpResponse
from query_pilot.client.types import Message, ToolSchema

BODIES = Path(__file__).resolve().parent / "provider_bodies"

#: `gemini-3.8-flash` refusing on its *daily* ceiling — the wall that does not clear until
#: midnight Pacific, and the reason a 429 cannot be treated as one condition.
GOOGLE_DAILY_429 = (BODIES / "google_daily_429.json").read_text()

#: `gemini-3.5-flash-lite` refusing on its per-minute ceiling. Derived, not verbatim; see
#: the README beside it.
GOOGLE_MINUTE_429 = (BODIES / "google_minute_429.json").read_text()

#: A model listed by the models endpoint that has been retired — the pinned model string
#: risk, arriving before any client code existed.
GOOGLE_RETIRED_404 = (BODIES / "google_retired_404.json").read_text()

#: Groq naming the ceiling it enforced, in prose rather than in a structured detail.
GROQ_RPM_429 = (BODIES / "groq_rpm_429.txt").read_text()

#: Reconstructed from the prose in docs/PROVIDERS.md: Cloudflare answers Python's default
#: User-Agent with an HTML page carrying this code and HTTP 403. The 0.4 inventory reported
#: every Groq model dead because of it. It is not an auth failure and must never be read as
#: one.
CLOUDFLARE_1010_403 = (
    "<html><head><title>Access denied</title></head><body><h1>error code: 1010</h1></body></html>"
)

#: Verbatim, from a Groq response. The names mislead: limit-requests is per *day* and
#: limit-tokens is per *minute*.
GROQ_RATE_LIMIT_HEADERS = {
    "x-ratelimit-limit-requests": "1000",
    "x-ratelimit-remaining-requests": "926",
    "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-tokens": "6130",
    "x-ratelimit-reset-requests": "1h46m33.599s",
    "x-ratelimit-reset-tokens": "14.025s",
}


# --- stubs ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Recorded:
    """One request the client tried to make."""

    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    provider: str
    model: str
    pool: str


@dataclass
class FakeHttp:
    """An HttpClient that answers from a script and remembers what it was asked.

    An entry that is an exception is raised instead of returned, which is how a timeout or
    a reset connection is put in front of the client without a socket.
    """

    replies: list[Any] = field(default_factory=list)
    requests: list[Recorded] = field(default_factory=list)
    closed: bool = False

    async def post(
        self,
        url: str,
        *,
        headers: Any,
        json: Any,
        provider: str,
        model: str,
        pool: str,
    ) -> HttpResponse:
        self.requests.append(Recorded(url, dict(headers), dict(json), provider, model, pool))
        if not self.replies:
            raise AssertionError(f"unscripted request to {provider}/{model} [{pool}]")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    async def aclose(self) -> None:
        self.closed = True

    @property
    def last(self) -> Recorded:
        return self.requests[-1]


def response(
    body: str, *, status: int = 200, headers: dict[str, str] | None = None, latency_s: float = 0.25
) -> HttpResponse:
    return HttpResponse(
        status=status, headers=headers or {}, body=body.encode(), latency_s=latency_s
    )


def groq_reply(
    text: str = "ready",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    prompt_tokens: int = 78,
    completion_tokens: int = 24,
    model: str = "openai/gpt-oss-20b",
    **kwargs: Any,
) -> HttpResponse:
    """A Groq success body, in the shape 0.4 recorded."""
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body = {
        "id": "chatcmpl-stub",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }
    return response(json.dumps(body), **kwargs)


def google_reply(
    text: str = "ready",
    *,
    function_calls: list[dict[str, Any]] | None = None,
    prompt_tokens: int = 8,
    completion_tokens: int = 1,
    model_version: str = "gemini-3.5-flash-lite-001",
    **kwargs: Any,
) -> HttpResponse:
    """A Google success body, in the shape 0.4 recorded."""
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"text": text})
    for call in function_calls or []:
        parts.append({"functionCall": call})
    body = {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": completion_tokens,
        },
        "modelVersion": model_version,
    }
    return response(json.dumps(body), **kwargs)


# --- neutral test material ---------------------------------------------------------------
# This package must not learn what this project uses it for, so nothing here is about
# databases or queries.


@pytest.fixture
def tool() -> ToolSchema:
    return ToolSchema(
        name="lookup_forecast",
        description="Look up the forecast for a place.",
        parameters={
            "type": "object",
            "properties": {"place": {"type": "string"}},
            "required": ["place"],
        },
    )


@pytest.fixture
def messages() -> list[Message]:
    return [
        Message(role="system", content="Answer briefly."),
        Message(role="user", content="What is the forecast for Pune?"),
    ]


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()
