"""Call the real providers once through the client, to check the adapters against live APIs.

**Never run from a test.** The suite makes no live API calls and passes with no keys; this
script exists so that "the adapters work" is a claim someone checked rather than inferred
from stubs that were written from the same reading of the documentation as the adapters.

It spends **four requests** by default — a text completion and a tool call on each of two
endpoints — against ceilings of 1,000 requests a day on Groq and 15 a minute on Google. It
writes nothing and reports what came back through the normalised `Completion`, so the two
providers' answers can be read side by side in the shape the rest of the project sees.

    set -a && . ./.env && set +a
    uv run python scripts/client_smoke.py
"""

from __future__ import annotations

import argparse
import asyncio

from query_pilot.client import ClientConfig, HttpxClient, Registry
from query_pilot.client.types import Message, ModelConfig, ToolSchema

DEFAULT_ENDPOINTS = ("groq/gpt-oss-20b", "google/gemini-3.5-flash-lite")

ASK = [Message(role="user", content="Reply with exactly one word: ready")]
ASK_WITH_TOOL = [Message(role="user", content="What is the forecast for Pune? Use the tool.")]
TOOL = ToolSchema(
    name="lookup_forecast",
    description="Look up the forecast for a place.",
    parameters={
        "type": "object",
        "properties": {"place": {"type": "string"}},
        "required": ["place"],
    },
)


async def probe(registry: Registry, endpoint_name: str) -> None:
    endpoint = registry.config.endpoints[endpoint_name]
    pools = registry.pools(endpoint.provider)
    if not pools:
        key = registry.config.providers[endpoint.provider].key_env
        print(f"{endpoint_name:<32} skipped, {key} not set")
        return
    adapter = registry.adapter(endpoint.provider, pools[0])
    config = ModelConfig(model=endpoint.model, max_output_tokens=128)

    text = await adapter.complete(ASK, None, config)
    called = await adapter.complete(ASK_WITH_TOOL, [TOOL], config)
    calls = ", ".join(f"{c.name}{dict(c.arguments)}" for c in called.tool_calls) or "none"
    print(
        f"{endpoint_name:<32} pool={text.pool:<20} served={text.model_returned}\n"
        f"{'':<32} text={text.text.strip()[:60]!r} "
        f"tokens={text.prompt_tokens}/{text.completion_tokens} {text.latency_s:.3f}s\n"
        f"{'':<32} tool_calls={calls}\n"
        f"{'':<32} rate_limit={text.rate_limit}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint",
        action="append",
        dest="endpoints",
        help="endpoint name from config/providers.toml; repeatable",
    )
    args = parser.parse_args()

    http = HttpxClient()
    registry = Registry(ClientConfig.load(), http)
    try:
        for name in args.endpoints or DEFAULT_ENDPOINTS:
            await probe(registry, name)
    finally:
        await http.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
