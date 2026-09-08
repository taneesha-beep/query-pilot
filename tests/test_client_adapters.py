"""What the two adapters send, and what they make of what comes back.

Normalising the difference between an OpenAI-shaped API and Google's is the work of this
item, so these tests are mostly about the difference rather than about either provider:
the same conversation and the same tool must produce two correctly-shaped requests, and
two differently-shaped responses must produce the same :class:`Completion`.

Every request here goes to a stub. No socket, no key.
"""

from __future__ import annotations

import json

import pytest

from conftest import (
    CLOUDFLARE_1010_403,
    GOOGLE_DAILY_429,
    GOOGLE_MINUTE_429,
    GOOGLE_RETIRED_404,
    GROQ_RATE_LIMIT_HEADERS,
    GROQ_RPM_429,
    FakeHttp,
    google_reply,
    groq_reply,
    response,
)
from query_pilot.client.errors import MalformedResponseError, ProviderHTTPError
from query_pilot.client.providers import GoogleProvider, OpenAICompatibleProvider
from query_pilot.client.providers.base import ProviderSpec, parse_duration_s
from query_pilot.client.types import Credential, Message, ModelConfig, ToolCall

GROQ_SPEC = ProviderSpec(
    name="groq",
    style="openai",
    url="https://api.groq.com/openai/v1/chat/completions",
    key_env="GROQ_API_KEY",
)
GOOGLE_SPEC = ProviderSpec(
    name="google-ai-studio",
    style="google",
    url="https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    key_env="GEMINI_API_KEY",
)
GROQ_KEY = Credential(pool="groq#1", env_var="GROQ_API_KEY", value="gsk-not-a-real-key")
GOOGLE_KEY = Credential(pool="google#1", env_var="GEMINI_API_KEY", value="AIza-not-a-real-key")

GROQ_MODEL = ModelConfig(model="openai/gpt-oss-20b", max_output_tokens=128, temperature=0.0)
GOOGLE_MODEL = ModelConfig(model="gemini-3.5-flash-lite", max_output_tokens=128, temperature=0.0)


def groq(http: FakeHttp) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(GROQ_SPEC, GROQ_KEY, http)


def google(http: FakeHttp) -> GoogleProvider:
    return GoogleProvider(GOOGLE_SPEC, GOOGLE_KEY, http)


# --- request shape ------------------------------------------------------------------


async def test_openai_request_wraps_each_tool_in_a_function_envelope(http, messages, tool):
    http.replies = [groq_reply()]
    await groq(http).complete(messages, [tool], GROQ_MODEL)

    sent = http.last.body
    assert sent["model"] == "openai/gpt-oss-20b"
    assert sent["max_completion_tokens"] == 128
    assert sent["temperature"] == 0.0
    assert sent["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup_forecast",
                "description": tool.description,
                "parameters": dict(tool.parameters),
            },
        }
    ]
    # A system message is a role here, and stays one.
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]


async def test_google_request_collects_tools_into_one_function_declarations_block(
    http, messages, tool
):
    http.replies = [google_reply()]
    await google(http).complete(messages, [tool], GOOGLE_MODEL)

    sent = http.last.body
    assert http.last.url.endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert sent["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "lookup_forecast",
                    "description": tool.description,
                    "parameters": dict(tool.parameters),
                }
            ]
        }
    ]
    assert sent["generationConfig"] == {"maxOutputTokens": 128, "temperature": 0.0}
    # A system message is not a role here. It is hoisted, and must not appear in contents.
    assert sent["systemInstruction"] == {"parts": [{"text": "Answer briefly."}]}
    assert [c["role"] for c in sent["contents"]] == ["user"]


async def test_credentials_travel_in_headers_and_never_in_the_url(http, messages):
    http.replies = [groq_reply(), google_reply()]
    await groq(http).complete(messages, None, GROQ_MODEL)
    assert http.last.headers["Authorization"] == "Bearer gsk-not-a-real-key"
    assert "gsk-not-a-real-key" not in http.last.url

    await google(http).complete(messages, None, GOOGLE_MODEL)
    assert http.last.headers["x-goog-api-key"] == "AIza-not-a-real-key"
    assert "AIza-not-a-real-key" not in http.last.url
    assert "key=" not in http.last.url


async def test_no_tools_means_no_tools_key_at_all(http, messages):
    http.replies = [groq_reply(), google_reply()]
    await groq(http).complete(messages, None, GROQ_MODEL)
    assert "tools" not in http.last.body
    await google(http).complete(messages, None, GOOGLE_MODEL)
    assert "tools" not in http.last.body


async def test_a_tool_result_can_be_replayed_to_either_provider(http, tool):
    # The turn after a tool call: the assistant's call, then its result. One provider
    # addresses the result by id and the other by function name, so a conversation that
    # can only be replayed to one of them would defeat the point of this package.
    call = ToolCall(id="call_7", name="lookup_forecast", arguments={"place": "Pune"})
    conversation = [
        Message(role="user", content="What is the forecast for Pune?"),
        Message(role="assistant", tool_calls=(call,)),
        Message(role="tool", content='{"sky": "clear"}', tool_call_id="call_7", name=call.name),
    ]

    http.replies = [groq_reply(), google_reply()]
    await groq(http).complete(conversation, [tool], GROQ_MODEL)
    sent = http.last.body["messages"]
    assert sent[1]["tool_calls"][0]["id"] == "call_7"
    # Arguments go back out as a JSON *string*, which is what this shape requires.
    assert json.loads(sent[1]["tool_calls"][0]["function"]["arguments"]) == {"place": "Pune"}
    assert sent[2] == {"role": "tool", "tool_call_id": "call_7", "content": '{"sky": "clear"}'}

    await google(http).complete(conversation, [tool], GOOGLE_MODEL)
    contents = http.last.body["contents"]
    assert contents[1] == {
        "role": "model",
        "parts": [{"functionCall": {"name": "lookup_forecast", "args": {"place": "Pune"}}}],
    }
    assert contents[2]["parts"][0]["functionResponse"] == {
        "name": "lookup_forecast",
        "response": {"sky": "clear"},
    }


async def test_a_non_object_tool_result_is_wrapped_rather_than_dropped(http):
    # Google requires functionResponse.response to be an object. A tool that returns a
    # bare string is a normal thing for a tool to do.
    conversation = [
        Message(role="tool", content="clear skies", tool_call_id="c1", name="lookup_forecast")
    ]
    http.replies = [google_reply()]
    await google(http).complete(conversation, None, GOOGLE_MODEL)
    assert http.last.body["contents"][0]["parts"][0]["functionResponse"]["response"] == {
        "result": "clear skies"
    }


# --- response normalisation ----------------------------------------------------------


async def test_the_same_answer_normalises_the_same_way_from_either_shape(http, messages):
    """One text answer, two wire formats, one Completion.

    Everything that differs between the providers is compared field by field; only the
    fields that genuinely differ — provider, pool, model, token counts — are left out.
    """
    http.replies = [groq_reply(text="ready"), google_reply(text="ready")]
    first = await groq(http).complete(messages, None, GROQ_MODEL)
    second = await google(http).complete(messages, None, GOOGLE_MODEL)

    assert first.text == second.text == "ready"
    assert first.tool_calls == second.tool_calls == ()
    assert first.provider == "groq"
    assert second.provider == "google-ai-studio"
    assert first.model == "openai/gpt-oss-20b"
    assert second.model == "gemini-3.5-flash-lite"
    assert first.pool == "groq#1"
    assert second.pool == "google#1"
    assert (first.prompt_tokens, first.completion_tokens) == (78, 24)
    assert (second.prompt_tokens, second.completion_tokens) == (8, 1)
    assert first.total_tokens == 102
    assert first.latency_s == second.latency_s == 0.25
    assert first.raw["choices"][0]["message"]["content"] == "ready"
    assert second.raw["candidates"][0]["content"]["parts"][0]["text"] == "ready"


async def test_a_tool_call_normalises_from_either_shape_with_arguments_as_a_mapping(
    http, messages, tool
):
    """The difference that would be a type error: a JSON string against an object."""
    http.replies = [
        groq_reply(
            text="",
            tool_calls=[
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "lookup_forecast",
                        "arguments": '{"place": "Pune"}',
                    },
                }
            ],
        ),
        google_reply(
            text="",
            function_calls=[{"name": "lookup_forecast", "args": {"place": "Pune"}}],
        ),
    ]
    first = await groq(http).complete(messages, [tool], GROQ_MODEL)
    second = await google(http).complete(messages, [tool], GOOGLE_MODEL)

    assert first.tool_calls[0].name == second.tool_calls[0].name == "lookup_forecast"
    assert first.tool_calls[0].arguments == second.tool_calls[0].arguments == {"place": "Pune"}
    assert first.tool_calls[0].id == "call_abc"
    # Google issues no id, so one is synthesised — a tool result has to be addressable.
    assert second.tool_calls[0].id == "lookup_forecast-0"


async def test_google_text_and_a_call_can_arrive_in_the_same_parts_list(http, messages, tool):
    http.replies = [
        google_reply(
            text="Looking that up. ",
            function_calls=[
                {"name": "lookup_forecast", "args": {"place": "Pune"}},
                {"name": "lookup_forecast", "args": {"place": "Nagpur"}},
            ],
        )
    ]
    completion = await google(http).complete(messages, [tool], GOOGLE_MODEL)
    assert completion.text == "Looking that up. "
    assert [call.arguments["place"] for call in completion.tool_calls] == ["Pune", "Nagpur"]
    # Ids come from the part's position, so two calls to one tool stay distinguishable.
    assert len({call.id for call in completion.tool_calls}) == 2


async def test_the_served_model_is_kept_apart_from_the_pinned_one(http, messages):
    http.replies = [google_reply(model_version="gemini-3.5-flash-lite-001")]
    completion = await google(http).complete(messages, None, GOOGLE_MODEL)
    assert completion.model == "gemini-3.5-flash-lite"
    assert completion.model_returned == "gemini-3.5-flash-lite-001"


async def test_groq_rate_limit_headers_are_read_by_meaning_not_by_name(http, messages):
    http.replies = [groq_reply(headers=GROQ_RATE_LIMIT_HEADERS)]
    limit = (await groq(http).complete(messages, None, GROQ_MODEL)).rate_limit
    assert limit is not None
    # limit-requests is per DAY and limit-tokens is per MINUTE. Reading these by their
    # names would put a 1,000-per-day ceiling into a per-minute bucket.
    assert limit.requests_per_day == 1000
    assert limit.requests_remaining == 926
    assert limit.tokens_per_minute == 8000
    assert limit.tokens_remaining == 6130
    assert limit.requests_reset_s == pytest.approx(6393.599)
    assert limit.tokens_reset_s == pytest.approx(14.025)


async def test_google_carries_no_rate_limit_facts_because_google_serves_none(http, messages):
    http.replies = [google_reply()]
    assert (await google(http).complete(messages, None, GOOGLE_MODEL)).rate_limit is None


# --- failures ---------------------------------------------------------------------------


async def test_tool_arguments_that_are_not_json_are_a_named_failure(http, messages, tool):
    # An agent that runs a tool with silently-dropped arguments is worse than one that
    # stops, so this must not degrade into an empty mapping.
    http.replies = [
        groq_reply(
            text="",
            tool_calls=[
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "lookup_forecast", "arguments": "{place: Pune"},
                }
            ],
        )
    ]
    with pytest.raises(MalformedResponseError, match="arguments are not JSON"):
        await groq(http).complete(messages, [tool], GROQ_MODEL)


async def test_a_body_that_is_not_json_is_a_named_failure(http, messages):
    http.replies = [response("<html>502 upstream</html>")]
    with pytest.raises(MalformedResponseError, match="not JSON"):
        await groq(http).complete(messages, None, GROQ_MODEL)


async def test_a_google_response_with_no_candidate_is_a_named_failure(http, messages):
    # What a prompt refused by a safety filter looks like: a 200 with promptFeedback and
    # no candidate. Not transport, not quota, and it must look like neither.
    http.replies = [response(json.dumps({"promptFeedback": {"blockReason": "SAFETY"}}))]
    with pytest.raises(MalformedResponseError, match="no candidates"):
        await google(http).complete(messages, None, GOOGLE_MODEL)


async def test_a_retired_model_carries_the_provider_s_own_explanation(http, messages):
    http.replies = [response(GOOGLE_RETIRED_404, status=404)]
    with pytest.raises(ProviderHTTPError) as raised:
        await google(http).complete(messages, None, GOOGLE_MODEL)
    assert raised.value.status == 404
    assert "no longer available" in raised.value.body
    assert raised.value.model == "gemini-3.5-flash-lite"


async def test_google_names_which_wall_it_hit_and_the_adapter_keeps_the_name(http, messages):
    """The distinction the whole retry design turns on, read off two real bodies."""
    http.replies = [
        response(GOOGLE_MINUTE_429, status=429),
        response(GOOGLE_DAILY_429, status=429),
    ]
    with pytest.raises(ProviderHTTPError) as minute:
        await google(http).complete(messages, None, GOOGLE_MODEL)
    with pytest.raises(ProviderHTTPError) as day:
        await google(http).complete(messages, None, GOOGLE_MODEL)

    assert minute.value.quota is not None and day.value.quota is not None
    assert minute.value.quota.quota_id == "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    assert minute.value.quota.quota_value == 15
    assert minute.value.quota.retry_after_s == pytest.approx(26.0)
    assert day.value.quota.quota_id == "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    assert day.value.quota.quota_value == 20
    assert day.value.quota.retry_after_s == pytest.approx(32.0)
    # Google's daily allowance for the models this project can afford is its most valuable
    # unmeasured number, and this field is where the first run to hit that wall reads it.
    assert day.value.quota.quota_metric == (
        "generativelanguage.googleapis.com/generate_content_free_tier_requests"
    )


async def test_groq_names_its_ceiling_in_prose_and_the_adapter_reads_it(http, messages):
    http.replies = [response(GROQ_RPM_429, status=429, headers={"retry-after": "2"})]
    with pytest.raises(ProviderHTTPError) as raised:
        await groq(http).complete(messages, None, GROQ_MODEL)
    quota = raised.value.quota
    assert quota is not None
    assert quota.quota_id == "RPM"
    assert quota.quota_value == 30
    assert quota.retry_after_s == pytest.approx(2.0)


async def test_a_retry_after_header_is_read_when_the_body_names_no_ceiling(http, messages):
    """The header path, on its own, with nothing in the body corroborating it.

    Groq's recorded refusal names its ceiling in prose *and* sends the header, so a test
    using that body cannot tell which of the two the adapter read. This one can: there is
    no ceiling in the body to find, and a delay still comes back.
    """
    http.replies = [response("Rate limited, mate.", status=429, headers={"retry-after": "7"})]
    with pytest.raises(ProviderHTTPError) as raised:
        await groq(http).complete(messages, None, GROQ_MODEL)
    quota = raised.value.quota
    assert quota is not None
    assert quota.retry_after_s == pytest.approx(7.0)
    # Nothing was invented to go with it. An unnamed ceiling stays unnamed, and quota
    # control decides what an unnamed wall costs.
    assert (quota.quota_id, quota.quota_metric, quota.quota_value) == (None, None, None)


async def test_the_header_outranks_the_bodys_own_hint_when_the_two_disagree(http, messages):
    """Two hints, one request. The transport-level one wins.

    A body's prose is written when the message is composed; the header is set when the
    response is sent, and it is the one an HTTP intermediary can correct. This is a
    smaller cousin of the rule quota control applies one level up, where the quota a body
    names outranks the retry hint the same body gives.
    """
    body = GROQ_RPM_429.replace("try again in 2s", "try again in 2m30s")
    http.replies = [response(body, status=429, headers={"retry-after": "9"})]
    with pytest.raises(ProviderHTTPError) as raised:
        await groq(http).complete(messages, None, GROQ_MODEL)
    quota = raised.value.quota
    assert quota is not None
    assert quota.retry_after_s == pytest.approx(9.0)
    # The ceiling itself still comes from the body, which is the only place it appears.
    assert (quota.quota_id, quota.quota_value) == ("RPM", 30)


async def test_a_429_naming_nothing_and_carrying_no_header_yields_no_quota_fact(http, messages):
    # An absent limit is unmodelled, not a limit of zero and not a limit of sixty seconds.
    # What to do with a wall nobody named is quota control's decision, not the adapter's.
    http.replies = [response("too many requests", status=429)]
    with pytest.raises(ProviderHTTPError) as raised:
        await groq(http).complete(messages, None, GROQ_MODEL)
    assert raised.value.status == 429
    assert raised.value.quota is None


async def test_tool_arguments_that_parse_but_are_not_an_object_are_a_named_failure(
    http, messages, tool
):
    # Valid JSON, and still unusable: a tool takes named arguments, so a bare array cannot
    # be spread over them. The near miss is worth its own case because it reaches a
    # different branch from a string that does not parse at all.
    http.replies = [
        groq_reply(
            text="",
            tool_calls=[
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "lookup_forecast", "arguments": '["Pune"]'},
                }
            ],
        )
    ]
    with pytest.raises(MalformedResponseError, match="arguments are not an object"):
        await groq(http).complete(messages, [tool], GROQ_MODEL)


@pytest.mark.parametrize("args", [["Pune"], "Pune", 7], ids=["a list", "a string", "a number"])
async def test_google_tool_arguments_that_are_not_an_object_are_a_named_failure(
    http, messages, tool, args
):
    """The same refusal on the other side of the normalisation, added by 1.5.

    Google states these arrive as an object and every call recorded at 0.4 did, so this
    side had no guard: the three shapes below escaped as a bare ``ValueError`` or
    ``TypeError``, which is not a :class:`ClientError` at all. Quota control would never
    have seen it to classify it, and a run would have filed a provider's malformed answer
    under ``executor_error`` — the one label 1.3 reserved for this project's own bugs.
    """
    http.replies = [
        google_reply(text="", function_calls=[{"name": "lookup_forecast", "args": args}])
    ]
    with pytest.raises(MalformedResponseError, match="arguments are not an object"):
        await google(http).complete(messages, [tool], GOOGLE_MODEL)


async def test_an_error_body_is_kept_whole(http, messages):
    # The per-minute body this suite needs was lost because the 0.4 probe truncated at 300
    # characters. A quota a provider names once is worth more than the bytes to store it.
    http.replies = [response(GOOGLE_DAILY_429, status=429)]
    with pytest.raises(ProviderHTTPError) as raised:
        await google(http).complete(messages, None, GOOGLE_MODEL)
    assert raised.value.body == GOOGLE_DAILY_429
    assert raised.value.observed_at.endswith("+00:00")


async def test_a_bot_block_is_carried_with_its_body_rather_than_read_as_auth(http, messages):
    # 403 with no auth failure anywhere in it. Classification is quota control's job; what
    # this adapter owes is the body it would need to tell them apart.
    http.replies = [response(CLOUDFLARE_1010_403, status=403)]
    with pytest.raises(ProviderHTTPError) as raised:
        await groq(http).complete(messages, None, GROQ_MODEL)
    assert raised.value.status == 403
    assert "error code: 1010" in raised.value.body
    assert raised.value.quota is None


async def test_an_exception_never_carries_the_key(http, messages):
    http.replies = [response(CLOUDFLARE_1010_403, status=403)]
    with pytest.raises(ProviderHTTPError) as raised:
        await groq(http).complete(messages, None, GROQ_MODEL)
    assert "gsk-not-a-real-key" not in str(raised.value)
    assert "gsk-not-a-real-key" not in repr(raised.value.headers)
    assert "gsk-not-a-real-key" not in repr(GROQ_KEY)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("2", 2.0),
        ("32s", 32.0),
        ("14.025s", 14.025),
        ("2h31m12s", 9072.0),
        ("1h46m33.599s", 6393.599),
        ("", None),
        (None, None),
        ("soon", None),
    ],
)
def test_provider_durations_parse(text, seconds):
    assert parse_duration_s(text) == (pytest.approx(seconds) if seconds is not None else None)
