"""The registry: logical name to provider, pinned model string and quota pool.

The item's acceptance is here — *the same call runs against both providers by changing one
config value* — and so is the convention the whole of quota control rests on: a suffixed
credential is its own pool, not a spare key for the same one.
"""

from __future__ import annotations

import inspect

import pytest

from conftest import FakeHttp, google_reply, groq_reply
from query_pilot.client.client import Client
from query_pilot.client.config import DEFAULT_CONFIG_PATH, ClientConfig, discover_pools
from query_pilot.client.errors import ConfigError
from query_pilot.client.registry import Registry
from query_pilot.client.types import Message

COMMITTED = DEFAULT_CONFIG_PATH.read_text()
BOTH_KEYS = {"GROQ_API_KEY": "gsk-stub", "GEMINI_API_KEY": "AIza-stub"}
ASK = [Message(role="user", content="What is the forecast for Pune?")]


def client_over(config_text: str, http: FakeHttp, environ=None, tmp_path=None) -> Client:
    path = tmp_path / "providers.toml"
    path.write_text(config_text)
    return Client(Registry(ClientConfig.load(path), http, environ or BOTH_KEYS))


# --- the committed configuration ------------------------------------------------------


def test_the_committed_configuration_pins_the_pair_from_the_inventory():
    config = ClientConfig.load()
    assert config.endpoints[config.roles["cheap"].endpoint].model == "openai/gpt-oss-20b"
    assert config.endpoints[config.roles["strong"].endpoint].model == "openai/gpt-oss-120b"
    # Recorded as observed at 0.4: Groq named 30 RPM in its own 429 body.
    assert config.endpoints["groq/gpt-oss-20b"].limits.rpm == 30
    assert config.endpoints["groq/gpt-oss-20b"].limits.sources["rpm"] == "observed"


def test_google_daily_limits_are_absent_rather_than_guessed():
    # Absent means UNMODELLED, not unlimited. Google publishes no per-model free-tier
    # figures and serves no rate-limit headers; its daily ceiling for the model this
    # project spills onto has never been reached, so there is no number to write.
    limits = ClientConfig.load().endpoints["google/gemini-3.5-flash-lite"].limits
    assert limits.rpm == 15
    assert limits.rpd is None
    assert limits.tpm is None
    assert limits.tpd is None
    # The one Google model whose daily ceiling *was* observed, by reading a 429 body.
    assert ClientConfig.load().endpoints["google/gemini-3.8-flash"].limits.rpd == 20


def test_no_call_site_can_name_a_model():
    # The mitigation for a provider retiring a model is that model strings live in one
    # file. That only holds if there is no way to pass one to a call.
    assert "model" not in inspect.signature(Client.complete).parameters


# --- the acceptance ---------------------------------------------------------------------


async def test_one_config_value_moves_a_role_to_the_other_provider(tmp_path, http, tool):
    """Acceptance for this item: the same call, two providers, one value changed.

    The call below is identical in both halves — same role, same messages, same tool. What
    differs is one line of the committed configuration file.
    """
    on_groq = COMMITTED
    # Anchored to the `cheap` role's own block: `cheap-no-spillover` (5.1) names the same
    # endpoint, and the acceptance is one value changed, not every line that matches.
    cheap_on_groq = '[roles.cheap]\nendpoint = "groq/gpt-oss-20b"'
    assert on_groq.count(cheap_on_groq) == 1
    on_google = on_groq.replace(
        cheap_on_groq, '[roles.cheap]\nendpoint = "google/gemini-3.5-flash-lite"'
    )

    http.replies = [
        groq_reply(
            text="",
            tool_calls=[
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "lookup_forecast", "arguments": '{"place": "Pune"}'},
                }
            ],
        ),
        google_reply(
            text="", function_calls=[{"name": "lookup_forecast", "args": {"place": "Pune"}}]
        ),
    ]

    first = await client_over(on_groq, http, tmp_path=tmp_path).complete("cheap", ASK, [tool])
    groq_request = http.last

    second = await client_over(on_google, http, tmp_path=tmp_path).complete("cheap", ASK, [tool])
    google_request = http.last

    # Two genuinely different wire shapes went out...
    assert groq_request.url.startswith("https://api.groq.com/")
    assert "tools" in groq_request.body and groq_request.body["tools"][0]["type"] == "function"
    assert google_request.url.startswith("https://generativelanguage.googleapis.com/")
    assert "functionDeclarations" in google_request.body["tools"][0]

    # ...and one shape came back.
    assert first.tool_calls[0].name == second.tool_calls[0].name == "lookup_forecast"
    assert first.tool_calls[0].arguments == second.tool_calls[0].arguments == {"place": "Pune"}
    assert (first.provider, first.model) == ("groq", "openai/gpt-oss-20b")
    assert (second.provider, second.model) == ("google-ai-studio", "gemini-3.5-flash-lite")


# --- credential pools ---------------------------------------------------------------------


def test_each_suffixed_key_is_its_own_pool():
    pools = discover_pools(
        "google-ai-studio",
        "GEMINI_API_KEY",
        {"GEMINI_API_KEY": "one", "GEMINI_API_KEY_2": "two"},
    )
    assert [p.pool for p in pools] == ["google-ai-studio#1", "google-ai-studio#2"]
    assert [p.env_var for p in pools] == ["GEMINI_API_KEY", "GEMINI_API_KEY_2"]
    # Two Google projects are two ceilings of 15 RPM, never one of 30. That is what the
    # 0.4 independence probe established, and what these separate pools carry forward.
    assert len({p.value for p in pools}) == 2


def test_a_gap_in_the_numbering_is_reported_rather_than_skipped():
    # _3 set with _2 unset is almost always a typo, and skipping past it quietly would
    # hide half of this project's Google capacity behind a misspelled variable name.
    with pytest.raises(ConfigError, match="GEMINI_API_KEY_3 is set but GEMINI_API_KEY_2"):
        discover_pools(
            "google-ai-studio",
            "GEMINI_API_KEY",
            {"GEMINI_API_KEY": "one", "GEMINI_API_KEY_3": "three"},
        )


def test_a_suffixed_key_without_a_base_key_is_reported():
    with pytest.raises(ConfigError, match="GEMINI_API_KEY_2 is set but GEMINI_API_KEY is not"):
        discover_pools("google-ai-studio", "GEMINI_API_KEY", {"GEMINI_API_KEY_2": "two"})


def test_a_blank_key_counts_as_unset():
    assert discover_pools("groq", "GROQ_API_KEY", {"GROQ_API_KEY": "   "}) == ()


def test_no_key_means_no_pools_not_an_error():
    assert discover_pools("groq", "GROQ_API_KEY", {}) == ()


# --- candidates ---------------------------------------------------------------------------


def test_candidates_run_primary_pools_first_then_spillover(http):
    registry = Registry(
        ClientConfig.load(),
        http,
        {"GROQ_API_KEY": "g1", "GEMINI_API_KEY": "a1", "GEMINI_API_KEY_2": "a2"},
    )
    assert [str(c) for c in registry.candidates("cheap")] == [
        "groq/gpt-oss-20b[groq#1]",
        "google/gemini-3.5-flash-lite[google-ai-studio#1]",
        "google/gemini-3.5-flash-lite[google-ai-studio#2]",
    ]
    # The strong tier has no spillover, and that emptiness is the single point of failure
    # named in docs/PROVIDERS.md rather than an oversight in this file.
    assert [str(c) for c in registry.candidates("strong")] == ["groq/gpt-oss-120b[groq#1]"]


def test_the_cheap_role_with_no_spillover_reaches_only_the_cheap_model(http):
    """Phase 5's cheap trajectories run here: every Groq pool of the cheap endpoint, and no
    other provider's model, however many keys are set."""
    registry = Registry(
        ClientConfig.load(),
        http,
        {
            "GROQ_API_KEY": "g1",
            "GROQ_API_KEY_2": "g2",
            "GEMINI_API_KEY": "a1",
            "GEMINI_API_KEY_2": "a2",
        },
    )
    assert [str(c) for c in registry.candidates("cheap-no-spillover")] == [
        "groq/gpt-oss-20b[groq#1]",
        "groq/gpt-oss-20b[groq#2]",
    ]


def test_a_candidate_is_keyed_on_provider_pool_and_model(http):
    registry = Registry(ClientConfig.load(), http, {"GROQ_API_KEY": "g1", "GEMINI_API_KEY": "a1"})
    cheap, spill = registry.candidates("cheap")[0], registry.candidates("cheap")[1]
    strong = registry.candidates("strong")[0]
    assert cheap.key == ("groq", "groq#1", "openai/gpt-oss-20b")
    assert strong.key == ("groq", "groq#1", "openai/gpt-oss-120b")
    assert spill.key == ("google-ai-studio", "google-ai-studio#1", "gemini-3.5-flash-lite")
    # Groq serves requests-per-day per model per organization and Google serves
    # requests-per-minute per model per project. Two models on one pool must not share a
    # ceiling, or quota control blocks work that is allowed.
    assert cheap.key != strong.key


def test_one_adapter_is_reused_per_provider_and_pool(http):
    registry = Registry(ClientConfig.load(), http, {"GROQ_API_KEY": "g1"})
    assert registry.candidates("cheap")[0].provider is registry.candidates("cheap")[0].provider


def test_a_role_with_no_key_anywhere_names_the_variable_to_set(http):
    registry = Registry(ClientConfig.load(), http, {})
    with pytest.raises(ConfigError, match="GEMINI_API_KEY, GROQ_API_KEY"):
        registry.candidates("cheap")


def test_an_unknown_role_lists_the_ones_that_exist(http):
    registry = Registry(ClientConfig.load(), http, BOTH_KEYS)
    with pytest.raises(ConfigError, match="configured roles: cheap, cheap-no-spillover, strong"):
        registry.candidates("thrifty")


# --- configuration errors ------------------------------------------------------------------


def test_an_endpoint_naming_a_provider_that_does_not_exist_is_reported(tmp_path):
    path = tmp_path / "providers.toml"
    path.write_text(COMMITTED.replace('provider = "groq"', 'provider = "grok"', 1))
    with pytest.raises(ConfigError, match="unknown provider 'grok'"):
        ClientConfig.load(path)


def test_an_endpoint_with_no_model_string_is_reported(tmp_path):
    path = tmp_path / "providers.toml"
    path.write_text(COMMITTED.replace('model = "openai/gpt-oss-20b"\n', "", 1))
    with pytest.raises(ConfigError, match="endpoint 'groq/gpt-oss-20b': missing 'model'"):
        ClientConfig.load(path)


def test_a_role_pointing_at_a_missing_endpoint_is_reported(tmp_path):
    path = tmp_path / "providers.toml"
    path.write_text(
        COMMITTED.replace(
            'spillover = ["google/gemini-3.5-flash-lite"]', 'spillover = ["google/retired"]'
        )
    )
    with pytest.raises(ConfigError, match="unknown endpoint 'google/retired'"):
        ClientConfig.load(path)


def test_a_missing_configuration_file_says_where_it_looked(tmp_path):
    with pytest.raises(ConfigError, match="no provider configuration at"):
        ClientConfig.load(tmp_path / "absent.toml")


def test_an_unknown_wire_style_is_reported(tmp_path, http):
    path = tmp_path / "providers.toml"
    path.write_text(COMMITTED.replace('style = "openai"', 'style = "anthropic-shaped"'))
    registry = Registry(ClientConfig.load(path), http, BOTH_KEYS)
    with pytest.raises(ConfigError, match="unknown wire style"):
        registry.candidates("cheap")


async def test_a_client_does_not_close_an_http_layer_it_was_given(http):
    client = Client(Registry(ClientConfig.load(), http, BOTH_KEYS))
    await client.aclose()
    assert http.closed is False


async def test_a_client_closes_the_http_layer_it_made_itself(http):
    # from_config with no http builds one and owns it. Constructing it opens no socket.
    client = Client.from_config(environ=BOTH_KEYS)
    assert client._owns_http is not None
    await client.aclose()
