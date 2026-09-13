# query-pilot

An agent that answers natural-language questions against relational databases it has not
been shown in full. It discovers a schema through tools, writes SQL, executes it in a
sandbox, and repairs its own query when execution fails. **Every claim made here is
verified by execution rather than by reading the SQL** — a query counts as solved when the
rows it returns match the rows the reference query returns, which is agreement with the
reference and not proof of a right answer (see Status below; this line said "is correct" until
2026-09-12). No model judges another model
anywhere in this project. The substrate is Spider dev; the agent is measured on a 150-task
working set, and a disjoint 150-task reserve set is read exactly once at the end.

## Results

Every figure below is `TBD` until it comes from a committed summary file produced by a
recorded run. A figure without a provider, a model string, a date and a ledger file behind
it does not appear in this table.

### Execution accuracy and cost

| Agent                                   | Execution accuracy       | Tokens per solved task | Provider | Model                                            | Date                    | Ledger                                     |
| --------------------------------------- | ------------------------ | ---------------------- | -------- | ------------------------------------------------ | ----------------------- | ------------------------------------------ |
| A0 single-shot                          | **124 / 150 — 82.6667%** | 860.8                  | Groq     | `openai/gpt-oss-120b`                            | 2026-09-08              | `runs/20260908-133316-faecd5/ledger.jsonl` |
| A1 agent                                | **116 / 150 — 77.3333%** | 6,646.8                | Groq     | `openai/gpt-oss-120b`                            | 2026-09-10              | `runs/20260910-024454-1f69bc/ledger.jsonl` |
| A2-cheap — A1's loop on the cheap model | **89 / 150 — 59.3333%**  | 9,492.9                | Groq     | `openai/gpt-oss-20b`                             | 2026-09-12              | `runs/20260912-055938-9712c8/ledger.jsonl` |
| A2 cascade — cheap, escalated to A1     | **117 / 150 — 78.0%**    | 10,032.3               | Groq     | `openai/gpt-oss-20b`, then `openai/gpt-oss-120b` | 2026-09-12 · 2026-09-10 | the A2-cheap and A1 ledgers above          |

**A query returning nothing scores 7 of 150 — 4.6667% — of this working set for free**,
because that many of its reference queries return no rows. That floor belongs beside the
accuracy figure above it rather than in a footnote, and here it does real work: **A0
collected all 7 of those tasks and A1 collected 2**, which is five of the eight tasks
between them.

**The loop lost, and it is reported that way.** A1 solved 8 fewer tasks than A0 and spent
**664,288 more tokens — 7.22×** — to do it, so there is no cost per additional solved task
because there are no additional solved tasks. The likeliest reason is the one this project
wrote down before measuring: **Spider's schemas fit in a prompt** — 4.5 tables and about
1,048 characters of DDL for the average working-set task — so discovery buys little and
costs a great deal. Excluding the seven free tasks the gap is three rather than eight.
**The two tasks A1 won are exactly the two [docs/FAILURES.md](docs/FAILURES.md) predicted
tools would win**, both of them A0 writing a literal that does not match the stored
spelling. Total cost: A0 106,740 tokens, A1 771,028, **$0.00 each** — both providers are
free tiers, so the cost that matters is quota.

**The cascade lost too, and by the rule written down before its figures existed.** A2 solves
one task more than A1 and costs **10,032.3 tokens a solved task against 6,646.8** — 1.52× the
tokens. The escalation rule is not why: on the cheap model it hands on 46 tasks, not one of which
the cheap model had solved, and catches 46 of its 61 failures. **The cheap model is simply not
cheaper here** — it spent 5,632.4 tokens a task against the strong model's 5,140.2, and every
token costs the same $0.00. What would flip it is a price: if a cheap token cost less than
**53.12%** of a strong one, the cascade would win. What it does save is the scarce model's quota
— 328,916 strong-model tokens against A1's 771,028.

Full write-up and the comparison tables in [docs/RESULTS.md](docs/RESULTS.md),
machine-readable in [results/a0-working.json](results/a0-working.json),
[results/a1-working.json](results/a1-working.json),
[results/a1-trajectory-metrics.json](results/a1-trajectory-metrics.json),
[results/a2-cheap-working.json](results/a2-cheap-working.json) and
[results/a2-working.json](results/a2-working.json).

### Execution accuracy by difficulty

| Agent          | easy     | medium   | hard     | extra    | Denominators      |
| -------------- | -------- | -------- | -------- | -------- | ----------------- |
| A0 single-shot | 91.6667% | 81.5385% | 76.0000% | 79.1667% | 36 / 65 / 25 / 24 |
| A1 agent       | 83.3333% | 75.3846% | 76.0000% | 75.0000% | 36 / 65 / 25 / 24 |

### Trajectory

| Measure                                         | A1                                         |
| ----------------------------------------------- | ------------------------------------------ |
| Tool calls per task (mean / median / p90)       | 4.3467 / 4 / 7, over 150                   |
| Turns to solve (mean / median / p90)            | 4.819 / 5 / 6, over the 116 solved         |
| **Recovery rate — first `execute_sql` errored** | **TBD over a denominator of 0**            |
| Recovery rate — first `execute_sql` empty       | 1 of 6 — 16.6667%                          |
| Wasted-call rate                                | 17 of 556 — 3.0576%, over 142 trajectories |
| Repairs attempted / succeeded                   | 1 / 1                                      |

A0 has no row here. A single-shot agent has no tool calls, no turns and no recovery, which
is the point of measuring them.

### Containment

| Measure                                                               | Value                  | Denominator                           |
| --------------------------------------------------------------------- | ---------------------- | ------------------------------------- |
| Compliance rate — the trajectory attempted the injected instruction   | **8 of 45 — 17.7778%** | every attack case                     |
| Containment rate — every compliant attempt refused before it executed | **5 of 5 — 100.0%**    | compliant cases a control can contain |
| Task-damage rate — resisted, and answered wrongly anyway              | **0 of 37 — 0.0%**     | cases the agent resisted              |

A1, Groq, `openai/gpt-oss-120b`, 2026-09-11, `runs/20260911-113246-1a97c9/ledger.jsonl`, over
the 45-case corpus. These three have different denominators and are never quoted as one
figure. **All five contained attempts were one shape** — `DROP TABLE audit_log`, refused by the
DDL/DML control before a connection opened — and no sandbox escape was attempted at all, so
100% is a thin claim. **Both wrong answers came from complying**, with an instruction no control
can contain. And **15 of the 45 cases were never seen**: A1 never looked inside the table that
carries the row-value injections. [docs/ATTACKS.md](docs/ATTACKS.md),
[results/attacks.json](results/attacks.json).

### Scheduler efficiency

| Run                                               | Running time | Ceiling from the declared quotas | Ratio        |
| ------------------------------------------------- | ------------ | -------------------------------- | ------------ |
| **A1** — Groq, `openai/gpt-oss-120b`, 2026-09-10  | 5,418.2073 s | 5,244.1274 s                     | **96.7871%** |
| A0 — Groq, `openai/gpt-oss-120b`, 2026-09-08      | 737.3566 s   | 734.4157 s                       | 99.6012%     |
| A2-cheap — Groq, `openai/gpt-oss-20b`, 2026-09-12 | 3,255.4176 s | 3,084.1555 s                     | 94.7392%     |

**A measurement of this project's scheduler, not of Groq's tiers**: the least time the declared
per-pool limits allow for the requests each run actually made, against the time the run took,
read from ledgers already written (`runs/20260910-024454-1f69bc/ledger.jsonl` and the other two)
without spending a request. The quotas, not the scheduler, set the pace; most of what A1 lost
sits in two sessions where Groq had already refused a pool for the day, and the scheduler's own
small loss is that it shuts such a pool for an hour when Groq says minutes. How long each request
waited is **derived, not recorded**, and the derivation cannot see the client correcting itself
to Groq's own counts — which turns out to be most of the waiting. How it was read was committed
before the figures existed (`d027fc7`). [docs/PERFORMANCE.md](docs/PERFORMANCE.md),
[results/scheduler-efficiency.json](results/scheduler-efficiency.json).

### Reserve set

Read once, after everything else is finished. It runs the best agent measured on the working
set, and **after Phase 5 that is still A0**: the highest agreement with the references (124 of 150) at the fewest tokens (860.8 a solved task). Neither A1 (116) nor the cascade (117) overtook
it.

| Agent | Execution accuracy | Date | Ledger |
| ----- | ------------------ | ---- | ------ |
| TBD   | TBD                | TBD  | TBD    |

## Status

**Phases 1 to 6 are complete, and Phase 7's viewer is built.** (This line said "Phases 1 to 4
are complete, and Phase 5's first item is" until 2026-09-12.) Phase 5 measured the cascade, which
lost; Phase 6 measured the scheduler against the ceiling its quotas allow and stated what no
number here claims ([docs/PERFORMANCE.md](docs/PERFORMANCE.md)), its load test having been cut;
and the trajectory viewer replays every committed run. The API, the deployment, this README's
last pass and the reserve run remain. Both agents exist, both have been measured over the whole
150-task working set on the same model, and every failure of both has been read by hand. The
execution surface is behind **five tested controls** ([`docs/GUARDRAILS.md`](docs/GUARDRAILS.md),
each with a test and a stated limit), and A1 has been measured against a **45-case
prompt-injection corpus** ([`attacks/corpus.json`](attacks/corpus.json)) planted in table and
column names, column type metadata and row values — the three figures above. Every failure mode
the project has a number for is catalogued with its frequency, its denominator and the artifact
it came from, apart from the ones seen but never counted, in
[docs/FAILURES.md](docs/FAILURES.md). No number in this README comes from anything but a
committed ledger.

**Phase 5 is measured: the cascade lost on cost per solved task** (the Results table above and
[docs/RESULTS.md](docs/RESULTS.md#a2--the-cascade--52)). How it got there:
A2 runs A1's loop on the cheap model (`openai/gpt-oss-20b`) and hands a task to the strong one
only when the cheap trajectory announces its own failure — it never answered, its answer failed
validation, or its first query errored or came back empty. The rule was committed before any
cascade run spent anything ([docs/ESCALATION.md](docs/ESCALATION.md)). Applied to A1's measured
run it would escalate 10 of 150 tasks and catches **9 of A1's 34 failures**, so a cascade can
only recover failures that announce themselves. Trying it on the cheap model first found what no
strong-model run had shown: **Groq refused the cheap model's own tool calls with an HTTP 400 on
6 of 19 trajectories** — arguments that were not JSON, a format token leaked into a tool's name
— and under this project's retry rule those were failed tasks the rule never saw. So the cheap
runs score such a refusal as an unsolved trajectory, `provider_rejected`, which the rule
escalates; A1's measured run, which retried its one such refusal, is unchanged. The always-cheap
run over the working set ran on 2026-09-12 and finished the same day, 150 of 150, after meeting
Groq's daily token limit on both pools at 105 and continuing on a third key. **How the cascade
would be read was committed before any of its figures existed** (`29464c1`) — composed from that
run and A1's, cost as every recorded token, and a verdict fixed in advance. And that limit
settled a question open since 3.6: Groq's tokens-per-day is a bucket that refills continuously,
all eight of its daily-limit refusals agreeing to the millisecond
([docs/PROVIDERS.md](docs/PROVIDERS.md)).

**A1 is measured and it lost.** 116 of 150 against A0's 124, for 7.22× the tokens, over six
sessions and one Groq daily quota wall. That is reported here the way it landed rather than
defended: the roadmap named the likely reason in advance and it is the right one, and what
A1 demonstrably has that A0 cannot — the ability to read the data before answering — is
worth two tasks on this substrate and is the thing Phase 4 measures under attack.

**What A1 is, and what is deliberately held constant.** It is given no schema: it discovers
one with `list_tables`, `describe_table`, `sample_rows` and `execute_sql`, each with an
explicit JSON schema, each returning a structured error rather than raising so the model can
repair itself inside a trajectory. Everything else is A0's — the same model on the same
`strong` role, the same output ceiling, the same answer rules word for word, the same
sandbox, and a final query re-executed and compared by the same equivalence rule. The loop is
the only thing that moved, which is the only condition under which the difference between the
two rows above is a statement about the loop.

Every turn, tool call and tool result is written to an append-only transcript, one file per
task, from which the exact conversation sent to the provider at any turn can be rebuilt. That
file is what the trajectory metrics, the containment measurement and the viewer all read.

**Its final answer is validated before it is executed** — exactly one statement, and that
statement must open a read-only query. A reply that fails gets **one** repair attempt with the
validation error fed back, and repair attempts and successes are counted as their own figures.
A trajectory that ran out of turns or tool calls gets no repair: repair answers _"you replied,
and the reply was not a single valid statement"_, not _"you never replied"_, and rescuing the
others would hide every trajectory that ran out of room behind an extra request.

**The four trajectory measures above are defined before they are taken**, in
`src/query_pilot/agents/metrics.py`, and every definition is written into the file the numbers
are published in. Two of them are easy to quote dishonestly and are not. **Recovery rate is
three numbers with three denominators** — the first query errored, the first query returned
nothing, or either — because a query returning nothing can be the correct answer, and a task
that never ran a query is in none of them. **The wasted-call rate is an upper bound on waste,
not a measurement of it**: a tool call that examined a table the final query never mentions is
counted wasted even when it was the call that ruled that table out.

**The same four tools are also an MCP server over stdio** — wrapped, not reimplemented, with a
test asserting that what crosses the wire is character for character what the in-process tool
returns. An external client gets exactly A1's reach and no more: a write is refused, the
timeout and caps still apply, and it reads a copy. [docs/MCP.md](docs/MCP.md) carries a
working client setup and what the dependency costs.

**A0 is a deliberately strong baseline**, because a weak one manufactures a result in A1's
favour. It gets the whole schema, read live from the database rather than from a dataset
annotation, in one prompt; it runs on the **same model A1 will**, so that the comparison
between them is about the agent loop and not about the model underneath it. What it does
not get is a second chance: one call, no tools, no repair.

The infrastructure those measurements ran on: an async client over two
providers and their quota pools, with token buckets keyed per pool per model, classified
retry that distinguishes a limit clearing in seconds from one clearing at midnight, and
spillover to another pool rather than waiting. On top of it, an append-only run ledger a
killed run resumes from without repeating or skipping a task, and a budget guard that stops
a run at a declared token or wall-clock ceiling and marks it incomplete — a summary derived
from an incomplete run carries no derived number at all, only the word `TBD` and the reason
it stopped.

And the measuring instrument itself, **written and committed before a single query was
generated**, because deciding what counts as a correct answer after seeing results is how a
project talks itself into a better number. Row order, column order, duplicate rows, NULL,
float tolerance, empty results and errors are each decided, priced and worked through in
[docs/EQUIVALENCE.md](docs/EQUIVALENCE.md). Applied to the 1,034 reference queries against
themselves it reports **100.00%**; permuting the rows of every result it compares leaves
every unordered answer solved and rejects every ordered one, which is the half of that
check with teeth.

Two things it establishes that qualify every accuracy figure this project will report.
**49 of the 1,034 reference queries return no rows**, so a query returning nothing scores
**4.7389%** by doing nothing at all. And every known limit of the rule — ties in an ordered
reference, positional column matching, multiset duplicates, no text-to-number coercion —
pushes the number **down**.

**And the reference queries have now been measured rather than asserted.** All 26 non-solves of
the A0 run were read by hand under a protocol fixed before the run started, in
[docs/FAILURES.md](docs/FAILURES.md). **Nine of the 26 are the reference query rather than
the model** — five of them returning demonstrably wrong data, each verified by running a
query, including two that compare a `TEXT` horsepower column against `150` and so count a
90-horsepower car as over 150. Nine more are the rule's own documented costs. Eight are the
model getting the data wrong, and none of the 26 was a malformed query or a wrong join.
**Nothing was re-scored**: 124 of 150 stands as taken, and what changes is how it is read.

**Which is not as a lower bound — this README said it was until 2026-09-12.** Reading all 34
of A1's failures the same way found the other direction. A defective reference does not only
reject correct answers; it accepts any wrong answer that reproduces its defect, and **at least 8
of A0's 124 solves are exactly that** — seven on `flight_2`, whose stored airport codes and
cities are padded with spaces so that the natural query and the reference return the same
nothing, and one where A0's literal matched nothing just as the reference's did. Each is
verified by a read-only query in [docs/a1-failure-counts.json](docs/a1-failure-counts.json).
"At least", because those were found without reading A0's solves. So each accuracy figure here
is **agreement with the reference queries**, which err in both directions: the rule's own costs
only push it down, and the references push it down and up. Half of A1's 34 failures are the
reference too, and in none of the ten tasks A1 lost and A0 won did A1 get the data wrong where
A0 got it right.

Provider limits, and which of them were measured against which were merely stated, are in
[docs/PROVIDERS.md](docs/PROVIDERS.md).

### The trajectory viewer

`viewer/` is a static page for watching an agent work rather than reading about it: pick a
working-set question, then A0, A1 or A2, and see each step in order — the tool called, the
arguments in, the result back — then the final statement, and a footer of what it cost: model,
whether the cascade escalated and why, tool calls, turns, tokens in and out, elapsed time
(which includes waiting for quota), which of the five controls fired, and whether the answer
**matches the reference**. Three attack cases are loaded first — one a control caught, one the
agent resisted, and one it complied with that no control can contain — each opening on the
poisoned schema.

**It replays committed runs and calls no model**: every figure comes from a committed file
through `src/query_pilot/viewer.py`, and `tests/test_viewer.py` rebuilds `viewer/data/` and
requires it back byte for byte. Its Run button is disabled until the local API (7.1) exists,
and the page never holds a key.

```
uv run python scripts/build_viewer.py
cd viewer && python -m http.server 8000
```

## Development

```
uv sync
uv run pytest
uv run ruff check .
```

The test suite makes no live API calls. It passes with no network and no keys present.
