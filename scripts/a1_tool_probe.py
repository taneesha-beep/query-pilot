"""Ask a real model to drive all four tools. **This spends real quota — a handful.**

**What is unproven, precisely.** 0.4's inventory recorded three Groq models calling a tool
and `docs/PROVIDERS.md` records that they all did — with a **one-function** schema. Nothing
in this repository has asked a model to *choose* among four, and four tools across several
turns is a different exercise. If `openai/gpt-oss-120b` chooses badly among these, the
whole of Phase 3 has a problem worth finding now rather than at 3.6, when finding it costs
a measured run.

Four questions, and none of them is answerable against a stub:

1. Does it call ``list_tables`` first, or guess a table name it was never shown?
2. Does it **chain** — take a result and call ``describe_table`` on something real?
3. **Does it put several tool calls in one message?** This one decides 3.2's turn limit. If
   eight ``describe_table`` calls fit in one turn, the turn limit can be tight; if each
   needs its own turn, the limit has to absorb them and 3.6 costs multiples more.
4. Does the arguments normalisation hold at four functions with typed parameters? Groq
   returns ``arguments`` as a JSON string and Google as an object, and 1.5 already found
   one missing guard on that asymmetry.

**No accuracy figure comes out of this.** It runs on `splits/smoke.json`, which is drawn
from inside the working set precisely so that probing costs a few tasks of a day's quota;
it never touches `splits/reserve.json`; and what it reports is tool names, call counts and
turn counts. Whether a trajectory happened to reach a right answer is not a result and is
not written down as one — 3.6 is the measured run.

**The loop here is deliberately not A1.** A1 is 3.2 and does not exist yet; this drives the
turns by hand so that 3.2's limits can be chosen against what a model actually does rather
than the other way round. The system prompt below is a draft for the same reason.

**The keys are not loaded for you.**

    set -a && . ./.env && set +a
    uv run python scripts/a1_tool_probe.py [--tasks 3] [--max-turns 3] [--max-requests 12]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from query_pilot.agents import MAX_OUTPUT_TOKENS, ROLE, extract_sql
from query_pilot.agents.tools import TOOL_SCHEMAS, call_tool
from query_pilot.client import Client, ClientError
from query_pilot.client.types import Message
from query_pilot.sandbox import CopyScope, Sandbox, SubstrateCopies
from query_pilot.tasks import load_tasks

REPO = Path(__file__).resolve().parent.parent
SPIDER = REPO / "data" / "spider"
SMOKE = REPO / "splits" / "smoke.json"
PROVIDERS = REPO / "config" / "providers.toml"
OUT = REPO / "docs" / "a1-tool-probe.json"


def model_string() -> str:
    """The pinned model the `strong` role resolves to, read rather than written down.

    No call site in this project names a model, and a record that named one would be the
    first. `roles.strong` has an empty spillover list, so the role resolves to exactly one
    endpoint and there is no second model this could have reached.
    """
    config = tomllib.loads(PROVIDERS.read_text())
    return str(config["endpoints"][config["roles"]["strong"]["endpoint"]]["model"])


#: A draft of A1's system prompt. 3.2 owns the final one; what matters here is that the
#: model is told it has not been shown the schema, which is the whole difference from A0.
DRAFT_SYSTEM_PROMPT = (
    "You are an expert SQLite analyst answering one question about one database.\n"
    "\n"
    "**You have not been shown the schema.** Use the tools to discover it: list_tables to "
    "see what exists, describe_table for the columns and keys of a table you intend to "
    "use, sample_rows when you need to see how a value is actually spelled, and "
    "execute_sql to check a query before you commit to it.\n"
    "\n"
    "When you know the answer, reply with a single SQLite SELECT statement and nothing "
    "else. Do not explain it and do not end it with a semicolon."
)


async def probe_one(client, sandbox, database, task, *, max_turns, spent, max_requests):
    """Drive one task by hand and record what the model did with the four tools."""
    messages = [
        Message(role="system", content=DRAFT_SYSTEM_PROMPT),
        Message(role="user", content=f"Database: {task.db_id}\n\nQuestion: {task.question}"),
    ]
    turns = []
    for turn in range(1, max_turns + 1):
        if spent[0] >= max_requests:
            return {
                "task_id": task.task_id,
                "db_id": task.db_id,
                "turns": turns,
                "stopped": "budget",
            }
        spent[0] += 1
        completion = await client.complete(
            ROLE, messages, TOOL_SCHEMAS, max_output_tokens=MAX_OUTPUT_TOKENS
        )
        calls = completion.tool_calls
        record = {
            "turn": turn,
            "tool_calls": [{"name": c.name, "arguments": dict(c.arguments)} for c in calls],
            "calls_in_this_turn": len(calls),
            "model": completion.model,
            "model_returned": completion.model_returned,
            "finish_reason": completion.finish_reason,
            "prompt_tokens": completion.prompt_tokens,
            "completion_tokens": completion.completion_tokens,
            "text_chars": len(completion.text),
        }
        if not calls:
            # The termination rule 3.2 will use: a turn with no tool calls is the answer.
            record["found_sql"] = extract_sql(completion.text).sql is not None
            turns.append(record)
            return {
                "task_id": task.task_id,
                "db_id": task.db_id,
                "turns": turns,
                "stopped": "answer",
            }

        messages.append(Message(role="assistant", content=completion.text, tool_calls=calls))
        results = []
        for call in calls:
            result = call_tool(sandbox, database, call)
            results.append({"name": call.name, "ok": result.ok, "error": result.error})
            messages.append(
                Message(
                    role="tool",
                    content=result.content,
                    tool_call_id=call.id,
                    name=call.name,
                )
            )
        record["results"] = results
        turns.append(record)
    return {"task_id": task.task_id, "db_id": task.db_id, "turns": turns, "stopped": "turn_limit"}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=3)
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--max-requests", type=int, default=12, help="hard ceiling on real calls")
    options = parser.parse_args()

    if not (SPIDER / "dev.json").exists():
        print("substrate not acquired; see docs/SUBSTRATE.md", file=sys.stderr)
        return 2

    tasks = load_tasks(SPIDER, SMOKE)[: options.tasks]
    client = Client.from_config()
    try:
        client.registry.candidates(ROLE)
    except ClientError as exc:
        print(f"{exc}\n\nLoad the keys first:  set -a && . ./.env && set +a", file=sys.stderr)
        return 2

    sandbox = Sandbox()
    spent = [0]
    probes = []
    with SubstrateCopies(SPIDER / "database", scope=CopyScope.RUN) as copies:
        for task in tasks:
            with copies.for_task(task.db_id, task_id=task.task_id) as database:
                probes.append(
                    await probe_one(
                        client,
                        sandbox,
                        database,
                        task,
                        max_turns=options.max_turns,
                        spent=spent,
                        max_requests=options.max_requests,
                    )
                )

    called = [c["name"] for p in probes for t in p["turns"] for c in t["tool_calls"]]
    report = {
        "provider": "groq",
        "role": ROLE,
        "model": model_string(),
        "date": datetime.now(UTC).date().isoformat(),
        "split": "smoke",
        "requests_spent": spent[0],
        "max_turns": options.max_turns,
        "first_tool_called": [
            p["turns"][0]["tool_calls"][0]["name"]
            if p["turns"] and p["turns"][0]["tool_calls"]
            else None
            for p in probes
        ],
        "tools_called_at_least_once": sorted(set(called)),
        "tools_never_called": sorted(set(s.name for s in TOOL_SCHEMAS) - set(called)),
        "calls_per_turn_max": max(
            (t["calls_in_this_turn"] for p in probes for t in p["turns"]), default=0
        ),
        "parallel_tool_calls_observed": any(
            t["calls_in_this_turn"] > 1 for p in probes for t in p["turns"]
        ),
        "stopped": [p["stopped"] for p in probes],
        "probes": probes,
        "what_this_is_not": (
            "Not a result. It runs on the smoke set, it reports tool and turn behaviour "
            "only, and no accuracy figure is taken from it. 3.6 is the measured run."
        ),
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "probes"}, indent=2))
    print(f"\nfull record: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
