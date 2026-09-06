"""Google AI Studio's generateContent REST API.

Every difference from the OpenAI-shaped adapter is a place a client that assumed one shape
would break:

- the key travels in an ``x-goog-api-key`` **header**, never in the URL, which is where
  Google's own quickstart puts it and where it would end up in logs and referrers;
- a tool schema goes in ``tools: [{"functionDeclarations": [...]}]``;
- a tool call comes back at ``candidates[0].content.parts[].functionCall`` with its
  arguments already an **object**, and a single response may carry text and calls in the
  same ``parts`` list;
- there is no call id, so one is synthesised from the call's position — a tool result has
  to be addressed to a specific call when the conversation is replayed to a provider that
  addresses results by id;
- system messages are not a role. They go in ``systemInstruction``;
- no rate-limit headers are served at all. What Google gives instead is a 429 body that
  names the quota it enforced, and that body is the only place the daily ceiling for the
  models this project can afford has ever appeared.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from query_pilot.client.errors import (
    MalformedResponseError,
    ProviderHTTPError,
    QuotaFact,
)
from query_pilot.client.http import HttpClient, HttpResponse
from query_pilot.client.providers.base import ProviderSpec, parse_duration_s
from query_pilot.client.types import (
    Completion,
    Credential,
    Message,
    ModelConfig,
    ToolCall,
    ToolSchema,
)

_QUOTA_FAILURE = "type.googleapis.com/google.rpc.QuotaFailure"
_RETRY_INFO = "type.googleapis.com/google.rpc.RetryInfo"


class GoogleProvider:
    """Google AI Studio on one credential pool, which for Google is one Cloud project."""

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
            self._spec.url.format(model=model_config.model),
            headers={
                "x-goog-api-key": self._credential.value,
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
        generation: dict[str, Any] = {"maxOutputTokens": model_config.max_output_tokens}
        if model_config.temperature is not None:
            generation["temperature"] = model_config.temperature
        if model_config.top_p is not None:
            generation["topP"] = model_config.top_p

        body: dict[str, Any] = {
            "contents": list(_contents(messages)),
            "generationConfig": generation,
        }
        instructions = [m.content for m in messages if m.role == "system" and m.content]
        if instructions:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(instructions)}]}
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": dict(tool.parameters),
                        }
                        for tool in tools
                    ]
                }
            ]
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
        candidates = body.get("candidates") or []
        if not candidates:
            # A prompt refused by a safety filter comes back exactly like this, with a
            # promptFeedback block and no candidate. It is not a transport problem and it
            # is not a quota problem, so it must not look like either.
            raise MalformedResponseError(
                "response carries no candidates",
                provider=self.name,
                model=model,
                pool=self.pool,
                body=response.text,
            )
        parts = (candidates[0].get("content") or {}).get("parts") or []
        usage = body.get("usageMetadata") or {}
        return Completion(
            text="".join(part.get("text", "") for part in parts if "text" in part),
            tool_calls=tuple(_tool_calls(parts)),
            prompt_tokens=usage.get("promptTokenCount"),
            completion_tokens=usage.get("candidatesTokenCount"),
            provider=self.name,
            model=model,
            pool=self.pool,
            latency_s=response.latency_s,
            model_returned=body.get("modelVersion"),
            finish_reason=candidates[0].get("finishReason"),
            rate_limit=None,
            raw=body,
        )

    # -- errors ----------------------------------------------------------------------
    def _http_error(self, response: HttpResponse, model: str) -> ProviderHTTPError:
        return ProviderHTTPError(
            status=response.status,
            body=response.text,
            provider=self.name,
            model=model,
            pool=self.pool,
            headers=response.headers,
            quota=quota_from(response.text),
        )


def _contents(messages: Sequence[Message]) -> Iterable[dict[str, Any]]:
    for message in messages:
        if message.role == "system":
            continue  # hoisted into systemInstruction
        if message.role == "tool":
            yield {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": message.name or "",
                            "response": _as_object(message.content),
                        }
                    }
                ],
            }
            continue
        role = "model" if message.role == "assistant" else "user"
        parts: list[dict[str, Any]] = []
        if message.content:
            parts.append({"text": message.content})
        parts.extend(
            {"functionCall": {"name": call.name, "args": dict(call.arguments)}}
            for call in message.tool_calls
        )
        yield {"role": role, "parts": parts or [{"text": ""}]}


def _as_object(content: str) -> dict[str, Any]:
    """A functionResponse must be an object; a tool result is a string.

    A result that already is a JSON object passes through, and anything else is wrapped
    rather than dropped or stringified into something that reads like an error.
    """
    try:
        parsed = json.loads(content)
    except ValueError:
        return {"result": content}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _tool_calls(parts: Sequence[Mapping[str, Any]]) -> Iterable[ToolCall]:
    for index, part in enumerate(parts):
        call = part.get("functionCall")
        if not call:
            continue
        name = call.get("name") or ""
        yield ToolCall(id=f"{name}-{index}", name=name, arguments=dict(call.get("args") or {}))


def quota_from(body: str) -> QuotaFact | None:
    """Read the quota Google named in a 429.

    ``quotaId`` is the field that separates a wall clearing in seconds from one clearing at
    midnight Pacific — ``GenerateRequestsPerMinutePerProjectPerModel-FreeTier`` against
    ``GenerateRequestsPerDayPerProjectPerModel-FreeTier``. It is kept verbatim: the daily
    allowance for the models this project can afford is still unmeasured, and the first run
    to hit that wall is what fills it in.
    """
    try:
        error = json.loads(body)["error"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(error, dict):
        return None

    quota_id = quota_metric = None
    quota_value: int | None = None
    retry_after: float | None = None
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        kind = detail.get("@type")
        if kind == _QUOTA_FAILURE:
            for violation in detail.get("violations") or []:
                quota_id = violation.get("quotaId") or quota_id
                quota_metric = violation.get("quotaMetric") or quota_metric
                raw_value = violation.get("quotaValue")
                if raw_value is not None:
                    try:
                        quota_value = int(raw_value)
                    except (TypeError, ValueError):
                        quota_value = None
        elif kind == _RETRY_INFO:
            retry_after = parse_duration_s(detail.get("retryDelay"))

    message = " ".join(str(error.get("message", "")).split())[:500]
    if quota_id is None and retry_after is None and not message:
        return None
    return QuotaFact(
        quota_id=quota_id,
        quota_metric=quota_metric,
        quota_value=quota_value,
        retry_after_s=retry_after,
        message=message,
    )
