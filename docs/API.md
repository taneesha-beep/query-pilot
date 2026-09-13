# The local API

A question typed against one of the working-set databases, answered by the agent on this
machine, its trajectory shown step by step in the viewer as it is written. **Local only.** The
server binds `127.0.0.1` and has no option to bind anything else, because a public URL holding
this project's keys would let any visitor spend the free tier the reserve run still needs
(constraint 79). What is deployed is the replay viewer; this serves the same page, from the
same files, with its Run button alive.

`src/query_pilot/api.py` is the service, `src/query_pilot/agents/live.py` the agent,
`config/runs/api.toml` the ceilings, `scripts/serve_api.py` the entry point, and
`tests/test_api.py` shows what every limit admits and refuses, with no key and no network.

## What it is, in one line each

| | |
|---|---|
| **Address** | `127.0.0.1` only, port 8765 by default |
| **Agents** | **A1** on `strong` (`openai/gpt-oss-120b`), or **A2-cheap** — A1's loop on `cheap-no-spillover` (`openai/gpt-oss-20b`) |
| **Databases** | the 20 working-set databases, by exact name |
| **Pool** | `GROQ_API_KEY` alone — `groq#1`. Never `groq#2`, `groq#3` or Google |
| **Verdict** | none: a typed question has no reference query, so nothing is scored |
| **Record** | every question is a run of one task under `runs/api/<run_id>/` — a ledger and a transcript |
| **Dependency** | `fastapi==0.141.1` and `uvicorn==0.52.4`, an **optional extra** — `uv sync --extra api` |

## Running it

```bash
set -a && . ./.env && set +a
uv run python scripts/serve_api.py
```

Then open `http://127.0.0.1:8765/`. The keys are not loaded for you (constraint 45); the server
checks the one key it uses is present before it listens. It serves `viewer/` at `/` and the API
under `/api/`.

## The Run button, and why a deployed copy of the page never enables it

The page's Run button is `disabled` in `viewer/index.html`. It comes alive only when both hold:

1. the page is on a loopback address — `127.0.0.1` or `localhost` — **and**
2. its own origin answers `api/status` as this API (`"surface": "local API"`, `"live": true`).

On any other host the page never asks, so a deployed copy makes no request beyond its own
files and logs no error. `tests/test_api.py` checks the gate in `app.js`'s source, shows a
static host serving `viewer/` answers `api/status` with 404, and requires that nothing under
`viewer/` is named `api`.

## Endpoints

| | |
|---|---|
| `GET /api/status` | the surface, the databases, the two agents and their models, the pool, the limits, each model's spend in the last 24 hours, and the run in flight |
| `POST /api/questions` | `{"database", "question", "agent"}` → `202` with a `run_id`, or a refusal |
| `GET /api/questions/{run_id}` | the trajectory so far, the final statement and its rows once finished, and the footer the viewer shows |

**Poll, not stream.** The page asks once a second — a turn averaged about 6.3 s on A1's
measured run. Each answer is read from the ledger and the transcript on disk, ordered by `seq`,
with a half-written last line left for the next read (`read_transcript(..., while_written=True)`),
so what the API returns is only ever the record, and it reads back the same after a restart.

## What a question runs, and what it does not

`LiveA1` is A1 with one step replaced. The loop, its limits (14 turns, 12 tool calls, one
repair), its prompt and its transcript are A1's own, and `A1` itself is unchanged, so nothing
that produced a committed figure can move. **What is replaced is scoring**: A1 executes its final
statement and the task's reference and compares them; a typed question has no reference. So
the final statement is executed once, and **through the guarded path** — `tools.execute_sql`,
where controls 4 and 5 fire before a connection opens — because a typed question is the
untrusted input constraint 81 guards against, and A1's own scoring path is deliberately
unguarded. What it returned goes in the task row's `detail` with `"scored": false`, and the
footer says **"no reference: not scored"**. **Any answer is a new draw**: nothing that builds a
committed file reads `runs/api/`, and a test says so.

A2-cheap is offered because 7.4's clip shows a repair: A1's measured run repaired 1 trajectory
of 150 on the strong model; A2-cheap's repaired 30 of 150 on the cheap one. Each agent runs
under the refusal rule its measured run declared — `fail_task` for A1, `score_unsolved` for
A2-cheap (constraint 89).

## The limits, and what each is derived from

Every one has a test showing the request it admits and the one it refuses.

| Limit | Value | Derived from |
|---|---|---|
| Databases | the 20 working-set databases, exact name | `results/a1-working.json`, a working-set file; Spider's folder holds 166 databases and a name is never turned into a path until it has matched |
| Question length | 1 to 1,000 characters | the longest working-set question is 152; at 1,000 the opening request is 9.1% of the prompt ceiling. A1's prompt ceiling never stops the first turn, so without this one question could overrun the per-minute token bucket in a single request (constraint 46) |
| Questions in flight | 1, across the server | constraints 46, 64 and 91: one attempt at the prompt ceiling is the strong endpoint's whole per-minute budget |
| Token ceiling, per question | 30,000 | the largest trajectory in either measured run — 25,401 tokens (A1, `dev-0549`) and 27,767 (A2-cheap, `dev-0484`) |
| Wall clock, per question | 300 s | the longest trajectory in either measured run — 172.9 s (A1, `dev-0549`) |
| Daily cap, per model | 93,260 tokens in any rolling 24 hours | Groq's 200,000 tokens a day per pool per model (constraint 95), less the 106,740 A0's working-set run spent (`results/a0-working.json`) — what the reserve run should need. The reserve run then fits even on the same pool on the same day |
| Set aside per question | 39,777 tokens | the 30,000 ceiling plus one request at the prompt ceiling, 9,777 on 120b (`docs/escalation-a1-working.json`), because the guard checks between turns. A question is refused once 53,484 tokens have been spent on its model in the last 24 hours |
| Per address | 10 questions an hour | 7.4's clip: with 30 of 150 cheap trajectories repaired, ten tries give an 89.3% chance of at least one repair. **On loopback every caller is the same address, so this is a second global limit**; the daily cap is what protects quota |

The daily cap is counted from the API's own ledgers under `runs/api/`, so a restart does not
reset it. A rolling sum is conservative against Groq's continuously refilling bucket. Polls and
status reads spend nothing and are not counted.

Where the figures above come from: A1's run is Groq, `openai/gpt-oss-120b`, 2026-09-10,
`runs/20260910-024454-1f69bc/ledger.jsonl` (`results/a1-working.json`); A2-cheap's is Groq,
`openai/gpt-oss-20b`, 2026-09-12, `runs/20260912-055938-9712c8/ledger.jsonl`
(`results/a2-cheap-working.json`).

## Keys

No key reaches a response: every body the API returns is scrubbed of every key value in the
environment it started from — all of them, not only the one it uses — and nothing it logs names
a header or a key. Validation errors name the fields that failed and never echo what was sent.
A test drives a question through the real client with sentinel keys and finds none of them in
any response, log line, transcript or ledger, and a second test has the provider echo the key
back in a 401 and finds it scrubbed from every response and log line.

**One limit, stated rather than hidden.** A task that fails records the provider's error message
in its ledger row (constraint 43), and that message quotes the first 200 characters of the
provider's reply. A provider that echoed a key would put it in that local, gitignored file. The
two providers this project uses do not.

## Live acceptance

2026-09-13, through the page served by the local API, typed by hand, on pool `groq#1`:

| Run | Agent | Model | Database | Requests | Tokens | Ended on |
|---|---|---|---|---|---|---|
| `20260913-052127-c68167` | A1 | `openai/gpt-oss-120b` | `concert_singer` | 5 | 4,033 | `answer` |
| `20260913-052201-2d5497` | A2-cheap | `openai/gpt-oss-20b` | `pets_1` | 4 | 2,998 | `answer` |

Nine requests of the thirty approved, none refused, no repair in either. No key value appeared
in either run directory or in the server's log. **Neither answer is a result**: acceptance shows
the path works, and the two questions were typed, not drawn from any split.
