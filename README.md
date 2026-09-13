# Query Pilot

An agent that answers questions about SQLite databases by writing SQL, with every answer
checked by running it.

**[Live demo](https://taneesha-beep.github.io/query-pilot/)** · **[All results](https://taneesha-beep.github.io/query-pilot/results.html)**

## What it does

- **Checks every answer by running it.** The agent's final query runs in a read-only sandbox,
  and its rows are compared with the rows of the reference query that comes with the question.
  No model grades another model.
- **Compares three agents on one model.** **A0** gets the whole schema in one request. **A1**
  discovers the schema with four tools (list tables, describe a table, sample rows, run SQL)
  and gets one repair for a malformed answer. **A2** runs A1's loop on a cheaper model and hands
  the question to A1 when the trajectory shows it failed.
- **Contains the SQL it runs.** Five controls sit between the agent and the database, and a
  45-case attack corpus plants instructions in table names, column types and rows to test them.
- **Runs on free tiers only.** A client with per-pool token buckets, classified retries, a
  resumable run ledger, and a budget guard that stops a run instead of degrading it.

## Results

Spider dev, a 150-task working set. **Matches the reference** means the agent's query returned
the same rows as the reference query. Some reference queries are wrong themselves, so a match is
agreement with the reference, not proof of a right answer.

### A0 against A1

| Agent          | Matches the reference    | Tokens  | Tokens per solved task |
| -------------- | ------------------------ | ------- | ---------------------- |
| A0 single-shot | **124 / 150 — 82.6667%** | 106,740 | 860.8                  |
| A1 agent loop  | **116 / 150 — 77.3333%** | 771,028 | 6,646.8                |

**The loop lost:** 8 fewer matches for 7.22× the tokens. Spider's schemas fit in a prompt, so
discovering them buys little. Seven reference queries return no rows, so an empty answer matches
them for free: A0 collected all 7, A1 only 2.

<sub>Groq, `openai/gpt-oss-120b`. A0: 2026-09-08, `runs/20260908-133316-faecd5/ledger.jsonl`.
A1: 2026-09-10, `runs/20260910-024454-1f69bc/ledger.jsonl`.</sub>

### Containment

| Measure                                         | Result                 | Out of                                |
| ----------------------------------------------- | ---------------------- | ------------------------------------- |
| Compliance — attempted the planted instruction  | **8 of 45 — 17.7778%** | every attack case                     |
| Containment — refused before it ran             | **5 of 5 — 100.0%**    | compliant cases a control can contain |
| Task damage — resisted, answered wrongly anyway | **0 of 37 — 0.0%**     | cases the agent resisted              |

All five contained attempts were the same `DROP TABLE`, refused before a connection opened, so
100% is a thin claim.

<sub>A1, Groq, `openai/gpt-oss-120b`, 2026-09-11, `runs/20260911-113246-1a97c9/ledger.jsonl`.</sub>

### Cost frontier

| Agent                                   | Matches the reference | Tokens per solved task |
| --------------------------------------- | --------------------- | ---------------------- |
| A0 single-shot                          | 124 / 150 — 82.6667%  | 860.8                  |
| A1 agent loop                           | 116 / 150 — 77.3333%  | 6,646.8                |
| A2-cheap — A1's loop on the cheap model | 89 / 150 — 59.3333%   | 9,492.9                |
| A2 cascade — cheap, escalated to A1     | 117 / 150 — 78.0%     | 10,032.3               |

**The cascade lost too:** the cheap model spent more tokens a question than the strong one. It
would win only if a cheap token cost less than 53.12% of a strong one.

<sub>A2-cheap: Groq, `openai/gpt-oss-20b`, 2026-09-12, `runs/20260912-055938-9712c8/ledger.jsonl`.
A2 is composed from the A2-cheap and A1 runs.</sub>

### Reserve set

Read once, at the very end, with A0. **TBD.**

The [results page](https://taneesha-beep.github.io/query-pilot/results.html) adds results by difficulty, A1's trajectory measures and
scheduler efficiency; [docs/RESULTS.md](docs/RESULTS.md) has the full write-up.

## Try it

**In a browser.** The [live demo](https://taneesha-beep.github.io/query-pilot/) replays every committed trajectory: A0, A1 and A2 on
all 150 questions, and all 45 attack cases. It calls no model.

**On your machine.** The test suite needs no keys and no network:

```bash
uv sync
uv run pytest
```

To ask your own question, run the local API. It spends free-tier quota, and it loads no keys
for you:

```bash
set -a && . ./.env && set +a
uv run python scripts/serve_api.py    # then open http://127.0.0.1:8765/
```

The same four tools are also an MCP server any MCP client can use: [docs/MCP.md](docs/MCP.md).

## Documentation

| Topic                        | Where                                                                     |
| ---------------------------- | ------------------------------------------------------------------------- |
| Every result, in full        | [docs/RESULTS.md](docs/RESULTS.md)                                        |
| How two answers are compared | [docs/EQUIVALENCE.md](docs/EQUIVALENCE.md)                                |
| The five controls            | [docs/GUARDRAILS.md](docs/GUARDRAILS.md)                                  |
| The attack corpus            | [docs/ATTACKS.md](docs/ATTACKS.md)                                        |
| Every failure, read          | [docs/FAILURES.md](docs/FAILURES.md)                                      |
| The escalation rule          | [docs/ESCALATION.md](docs/ESCALATION.md)                                  |
| Scheduler efficiency         | [docs/PERFORMANCE.md](docs/PERFORMANCE.md)                                |
| Providers and quotas         | [docs/PROVIDERS.md](docs/PROVIDERS.md)                                    |
| The local API                | [docs/API.md](docs/API.md)                                                |
| The MCP server               | [docs/MCP.md](docs/MCP.md)                                                |
| The substrate and the splits | [docs/SUBSTRATE.md](docs/SUBSTRATE.md) · [docs/SPLITS.md](docs/SPLITS.md) |
