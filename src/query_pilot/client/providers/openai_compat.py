"""The OpenAI-shaped chat completions API. Groq speaks it.

Two things about this wire format are worth stating because they are exactly where the
other adapter differs:

- a tool schema is wrapped, ``{"type": "function", "function": {...}}``;
- a tool call's ``arguments`` arrive as a **JSON string**, not an object, and a model that
  emits something that is not valid JSON is a real failure rather than a hypothetical one.
  It becomes :class:`MalformedResponseError` instead of a quietly empty argument mapping,
  because an agent that runs a tool with silently-dropped arguments is worse than one that
  stops.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from query_pilot.client.errors import (
    MalformedResponseError,
    ProviderHTTPError,
    QuotaFact,
)
from query_pilot.client.http import HttpClient, HttpResponse
from query_pilot.client.providers.base import (
    ProviderSpec,
    generation_options,
    parse_duration_s,
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

# Groq names the ceiling it enforced in prose:
#   "on requests per minute (RPM): Limit 30, Used 30, Requested 1. Please try again in 2s."
_LIMIT = re.compile(r"on (?P<metric>[a-z ]+) \((?P<abbr>RPM|RPD|TPM|TPD)\): Limit (?P<value>\d+)")
_TRY_AGAIN = re.compile(r"try again in ([0-9hms.]+)")


class OpenAICompatibleProvider:
    """One provider speaking the OpenAI chat-completions shape, on one credential pool."""

    def __init__(self, spec: ProviderSpec, credential: Credential, http: HttpClient) -> None:
        self._spec = spec
        self._credential = credential
        self._http = http
        self.name = spec.name
        self.pool = credential.pool

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None,
        model_config: ModelConfig,
    ) -> Completion:
        response = await self._http.post(
            self._spec.url,
            headers={
                "Authorization": f"Bearer {self._credential.value}",
                "Content-Type": "application/json",
            },
            json=self.request_body(messages, tools, model_config),
            provider=self.name,
            model=model_config.model,
            pool=self.pool,
        )
        if response.status != 200:
            raise self._http_error(response, model_config.model)
        return self._read(response, model_config.model)

    # -- request ---------------------------------------------------------------------
    def request_body(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None,
        model_config: ModelConfig,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model_config.model,
            "messages": [_message(message) for message in messages],
            "max_completion_tokens": model_config.max_output_tokens,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.parameters),
                    },
                }
                for tool in tools
            ]
        body.update(generation_options(model_config))
        body.update(model_config.extra)
        return body

    # -- response --------------------------------------------------------------------
    def _read(self, response: HttpResponse, model: str) -> Completion:
        try:
            body = json.loads(response.body)
        except ValueError as exc:
            raise MalformedResponseError(
                f"response body is not JSON: {exc}",
                provider=self.name,
                model=model,
                pool=self.pool,
                body=response.text,
            ) from exc
        choices = body.get("choices") or []
        if not choices:
            raise MalformedResponseError(
                "response carries no choices",
                provider=self.name,
                model=model,
                pool=self.pool,
                body=response.text,
            )
        message = choices[0].get("message") or {}
        usage = body.get("usage") or {}
        return Completion(
            text=message.get("content") or "",
            tool_calls=tuple(self._tool_calls(message, model, response.text)),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            provider=self.name,
            model=model,
            pool=self.pool,
            latency_s=response.latency_s,
            model_returned=body.get("model"),
            finish_reason=choices[0].get("finish_reason"),
            rate_limit=rate_limit_from(response.headers),
            raw=body,
        )

    def _tool_calls(self, message: Mapping[str, Any], model: str, body: str) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, Mapping):
                arguments: Mapping[str, Any] = dict(raw_arguments)
            else:
                try:
                    parsed = json.loads(raw_arguments or "{}")
                except ValueError as exc:
                    raise MalformedResponseError(
                        f"tool call {function.get('name')!r} arguments are not JSON: {exc}",
                        provider=self.name,
                        model=model,
                        pool=self.pool,
                        body=body,
                    ) from exc
                if not isinstance(parsed, Mapping):
                    raise MalformedResponseError(
                        f"tool call {function.get('name')!r} arguments are not an object",
                        provider=self.name,
                        model=model,
                        pool=self.pool,
                        body=body,
                    )
                arguments = dict(parsed)
            calls.append(
                ToolCall(
                    id=call.get("id") or f"{function.get('name', 'tool')}-{index}",
                    name=function.get("name") or "",
                    arguments=arguments,
                )
            )
        return calls

    # -- errors ----------------------------------------------------------------------
    def _http_error(self, response: HttpResponse, model: str) -> ProviderHTTPError:
        return ProviderHTTPError(
            status=response.status,
            body=response.text,
            provider=self.name,
            model=model,
            pool=self.pool,
            headers=response.headers,
            quota=quota_from(response.text, response.headers),
        )


def _message(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content,
        }
    entry: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        entry["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(dict(call.arguments))},
            }
            for call in message.tool_calls
        ]
    return entry


def rate_limit_from(headers: Mapping[str, str]) -> RateLimit | None:
    """Read Groq's rate-limit headers, whose names mislead.

    Its documentation fixes the meaning: ``x-ratelimit-limit-requests`` is requests per
    **day** and ``x-ratelimit-limit-tokens`` is tokens per **minute**. Inferring from the
    names would put a 1,000-per-day ceiling into a per-minute bucket.
    """

    def as_int(key: str) -> int | None:
        raw = headers.get(key)
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    limit = RateLimit(
        requests_per_day=as_int("x-ratelimit-limit-requests"),
        requests_remaining=as_int("x-ratelimit-remaining-requests"),
        requests_reset_s=parse_duration_s(headers.get("x-ratelimit-reset-requests")),
        tokens_per_minute=as_int("x-ratelimit-limit-tokens"),
        tokens_remaining=as_int("x-ratelimit-remaining-tokens"),
        tokens_reset_s=parse_duration_s(headers.get("x-ratelimit-reset-tokens")),
    )
    return None if limit == RateLimit() else limit


def quota_from(body: str, headers: Mapping[str, str]) -> QuotaFact | None:
    """Pull the named ceiling out of a Groq error body.

    The abbreviation is kept as the provider wrote it. Whether ``RPM`` means wait two
    seconds and whether ``RPD`` means wait until tomorrow is a decision for quota control,
    not for a parser.
    """
    retry_after = parse_duration_s(headers.get("retry-after"))
    match = _LIMIT.search(body)
    if match is None:
        if retry_after is None:
            return None
        return QuotaFact(retry_after_s=retry_after, message=_summary(body))
    again = _TRY_AGAIN.search(body)
    return QuotaFact(
        quota_id=match.group("abbr"),
        quota_metric=match.group("metric").strip(),
        quota_value=int(match.group("value")),
        retry_after_s=retry_after
        if retry_after is not None
        else parse_duration_s(again.group(1) if again else None),
        message=_summary(body),
    )


def _summary(body: str) -> str:
    return " ".join(body.split())[:500]
