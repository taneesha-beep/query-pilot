"""Verify provider free tiers by making real calls, and record what came back.

Writes docs/provider-check.json. **Never run from a test.** The test suite makes no live
API calls; this script exists so that docs/PROVIDERS.md contains numbers that were
observed rather than quoted.

Reads keys from the environment (GROQ_API_KEY, CEREBRAS_API_KEY, GEMINI_API_KEY) and
writes none of them anywhere. Keys are sent in headers, never in a URL.

Three probes per model:
  1. a small chat completion, for latency, token usage and the model string echoed back;
  2. a tool-call probe, because Phase 3's agent is tools all the way down and a model that
     cannot call a tool is not a candidate however cheap it is;
  3. a short concurrent burst on one model per provider, to see the requests-per-minute
     ceiling behave rather than take the documentation's word for it.

Run: uv run python scripts/provider_check.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "provider-check.json"
BURST = 15

# Groq and Cerebras sit behind Cloudflare, which answers Python's default User-Agent with
# a 1010 bot block that looks exactly like an auth failure. Phase 1's client must send a
# real one too; finding this at 0.4 rather than at 1.1 is what this item is for.
USER_AGENT = "query-pilot/0.1 (+https://github.com/taneesha-beep/query-pilot)"

# Rate-limit facts a provider chooses to serve back. Recorded verbatim where present.
RATE_LIMIT_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
    "retry-after",
)

PROVIDERS: dict[str, dict[str, Any]] = {
    "groq": {
        "style": "openai",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "models": ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
        "burst_models": {"openai/gpt-oss-20b": 40, "openai/gpt-oss-120b": 40},
    },
    "cerebras": {
        "style": "openai",
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "key_env": "CEREBRAS_API_KEY",
        "models": ["qwen-3.8-27b", "gpt-oss-120b", "gemma-4-31b"],
        "burst_models": {"qwen-3.8-27b": 15},
    },
    "google-ai-studio": {
        "style": "gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "key_env": "GEMINI_API_KEY",
        "models": ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-2.5-flash"],
        "burst_models": {"gemini-3.5-flash-lite": 20, "gemini-3.8-flash": 20},
    },
}

QUESTION = "Reply with exactly one word: ready"
TOOL_QUESTION = "List the tables in the database. Use the tool."
TOOL_SCHEMA = {
    "name": "list_tables",
    "description": "List the tables in the database.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def _request(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(body).encode()
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            elapsed = time.monotonic() - started
            return {
                "status": response.status,
                "latency_s": round(elapsed, 3),
                "headers": {
                    h: response.headers.get(h)
                    for h in RATE_LIMIT_HEADERS
                    if response.headers.get(h) is not None
                },
                "body": json.loads(response.read()),
            }
    except urllib.error.HTTPError as exc:
        # Gemini names the quota it enforced deep in the body; do not truncate it away.
        detail = exc.read().decode(errors="replace")[:2000]
        return {
            "status": exc.code,
            "latency_s": round(time.monotonic() - started, 3),
            "headers": {
                h: exc.headers.get(h) for h in RATE_LIMIT_HEADERS if exc.headers.get(h) is not None
            },
            "error": detail,
        }
    except Exception as exc:
        return {
            "status": None,
            "latency_s": round(time.monotonic() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }


def call(
    provider: str, model: str, *, with_tool: bool = False, tokens: int = 128
) -> dict[str, Any]:
    spec = PROVIDERS[provider]
    key = os.environ[spec["key_env"]]
    if spec["style"] == "openai":
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": TOOL_QUESTION if with_tool else QUESTION}],
            "max_completion_tokens": tokens,
        }
        if with_tool:
            body["tools"] = [{"type": "function", "function": TOOL_SCHEMA}]
        return _request(
            spec["url"],
            {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            body,
        )

    body = {
        "contents": [
            {"role": "user", "parts": [{"text": TOOL_QUESTION if with_tool else QUESTION}]}
        ],
        "generationConfig": {"maxOutputTokens": tokens},
    }
    if with_tool:
        body["tools"] = [{"functionDeclarations": [TOOL_SCHEMA]}]
    return _request(
        spec["url"].format(model=model),
        {"x-goog-api-key": key, "Content-Type": "application/json", "User-Agent": USER_AGENT},
        body,
    )


def summarise(provider: str, result: dict[str, Any]) -> dict[str, Any]:
    """Pull the few facts worth recording out of a raw response."""
    out = {k: result.get(k) for k in ("status", "latency_s", "headers", "error")}
    body = result.get("body")
    if not body:
        return out
    if PROVIDERS[provider]["style"] == "openai":
        usage = body.get("usage") or {}
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        out["model_returned"] = body.get("model")
        out["prompt_tokens"] = usage.get("prompt_tokens")
        out["completion_tokens"] = usage.get("completion_tokens")
        out["finish_reason"] = choice.get("finish_reason")
        out["tool_calls"] = [
            c.get("function", {}).get("name") for c in (message.get("tool_calls") or [])
        ]
        out["text"] = (message.get("content") or "").strip()[:120]
    else:
        usage = body.get("usageMetadata") or {}
        candidate = (body.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        out["model_returned"] = body.get("modelVersion")
        out["prompt_tokens"] = usage.get("promptTokenCount")
        out["completion_tokens"] = usage.get("candidatesTokenCount")
        out["finish_reason"] = candidate.get("finishReason")
        out["tool_calls"] = [p["functionCall"]["name"] for p in parts if "functionCall" in p]
        out["text"] = "".join(p.get("text", "") for p in parts).strip()[:120]
    return out


def _limit_named_in(throttled: list[dict[str, Any]]) -> str | None:
    """Read the limit out of a 429 body. Both providers say which ceiling was hit."""
    for result in throttled:
        body = result.get("error") or ""
        groq = re.search(r"requests per minute \(RPM\): Limit (\d+)", body)
        if groq:
            return f"RPM={groq.group(1)}"
        try:
            for detail in json.loads(body)["error"].get("details", []):
                for violation in detail.get("violations", []):
                    if violation.get("quotaValue"):
                        return f"{violation.get('quotaId')}={violation['quotaValue']}"
        except (ValueError, KeyError, TypeError):
            continue
    return None


def burst(provider: str, model: str, size: int = BURST) -> dict[str, Any]:
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=size) as pool:
        results = list(pool.map(lambda _: call(provider, model, tokens=32), range(size)))
    elapsed = time.monotonic() - started
    statuses: dict[str, int] = {}
    for r in results:
        statuses[str(r["status"])] = statuses.get(str(r["status"]), 0) + 1
    throttled = [r for r in results if r["status"] == 429]
    return {
        "requests": size,
        "limit_named_by_provider": _limit_named_in(throttled),
        "concurrent": True,
        "wall_clock_s": round(elapsed, 3),
        "status_counts": statuses,
        "throttled": len(throttled),
        "retry_after_seen": sorted({r["headers"].get("retry-after") for r in throttled} - {None}),
        "throttle_messages": sorted(
            {" ".join((r.get("error") or "").split())[:300] for r in throttled}
        ),
        "headers_last": next((r["headers"] for r in reversed(results) if r.get("headers")), {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--burst", type=int, default=0, help="override every burst size")
    parser.add_argument("--burst-only", action="store_true", help="skip the per-model probes")
    parser.add_argument("--out", default=str(OUT), help="where to write the report")
    args = parser.parse_args()

    report: dict[str, Any] = {
        "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "burst_size": args.burst,
    }
    for provider, spec in PROVIDERS.items():
        if not os.environ.get(spec["key_env"]):
            report[provider] = {"skipped": f"{spec['key_env']} not set"}
            print(f"{provider}: skipped, no key")
            continue
        entry: dict[str, Any] = {"models": {}}
        for model in [] if args.burst_only else spec["models"]:
            completion = summarise(provider, call(provider, model))
            tool = summarise(provider, call(provider, model, with_tool=True, tokens=256))
            entry["models"][model] = {"completion": completion, "tool_call": tool}
            ok = completion["status"] == 200
            calls = tool.get("tool_calls") or []
            print(
                f"{provider:>17} {model:<26} "
                f"chat={'ok' if ok else completion['status']} "
                f"{completion.get('latency_s')}s "
                f"tools={'ok' if calls else 'none'} "
                f"tokens={completion.get('prompt_tokens')}/{completion.get('completion_tokens')}"
            )
        entry["bursts"] = {}
        for model, size in spec["burst_models"].items():
            probe = burst(provider, model, size=args.burst or size)
            entry["bursts"][model] = probe
            named = probe["limit_named_by_provider"] or "no limit reached"
            print(
                f"{provider:>17} burst {model:<26} {probe['status_counts']} "
                f"in {probe['wall_clock_s']}s -> {named}"
            )
        report[provider] = entry

    destination = Path(args.out)
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nwritten: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
