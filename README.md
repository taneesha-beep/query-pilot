# query-pilot

An agent that answers natural-language questions against relational databases it has not
been shown in full. It discovers a schema through tools, writes SQL, executes it in a
sandbox, and repairs its own query when execution fails. **Every claim made here is
verified by execution rather than by reading the SQL** — a query is correct when the rows
it returns match the rows the reference query returns. No model judges another model
anywhere in this project. The substrate is Spider dev; the agent is measured on a 150-task
working set, and a disjoint 150-task reserve set is read exactly once at the end.

## Results

Every figure below is `TBD` until it comes from a committed summary file produced by a
recorded run. A figure without a provider, a model string, a date and a ledger file behind
it does not appear in this table.

### Execution accuracy and cost

| Agent | Execution accuracy | Tokens per solved task | Provider | Model | Date | Ledger |
|---|---|---|---|---|---|---|
| A0 single-shot | **124 / 150 — 82.6667%** | 860.8 | Groq | `openai/gpt-oss-120b` | 2026-09-08 | `runs/20260908-133316-faecd5/ledger.jsonl` |
| A1 agent | **116 / 150 — 77.3333%** | 6,646.8 | Groq | `openai/gpt-oss-120b` | 2026-09-10 | `runs/20260910-024454-1f69bc/ledger.jsonl` |
| A2 cascade | TBD | TBD | TBD | TBD | TBD | TBD |

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

Full write-up and the comparison table in [docs/RESULTS.md](docs/RESULTS.md),
machine-readable in [results/a0-working.json](results/a0-working.json),
[results/a1-working.json](results/a1-working.json) and
[results/a1-trajectory-metrics.json](results/a1-trajectory-metrics.json).

### Execution accuracy by difficulty

| Agent | easy | medium | hard | extra | Denominators |
|---|---|---|---|---|---|
| A0 single-shot | 91.6667% | 81.5385% | 76.0000% | 79.1667% | 36 / 65 / 25 / 24 |
| A1 agent | 83.3333% | 75.3846% | 76.0000% | 75.0000% | 36 / 65 / 25 / 24 |

### Trajectory

| Measure | A1 |
|---|---|
| Tool calls per task (mean / median / p90) | 4.3467 / 4 / 7, over 150 |
| Turns to solve (mean / median / p90) | 4.819 / 5 / 6, over the 116 solved |
| **Recovery rate — first `execute_sql` errored** | **TBD over a denominator of 0** |
| Recovery rate — first `execute_sql` empty | 1 of 6 — 16.6667% |
| Wasted-call rate | 17 of 556 — 3.0576%, over 142 trajectories |
| Repairs attempted / succeeded | 1 / 1 |

A0 has no row here. A single-shot agent has no tool calls, no turns and no recovery, which
is the point of measuring them.

### Containment

| Measure | Value | Denominator |
|---|---|---|
| Compliance rate — the trajectory attempted the injected instruction | **8 of 45 — 17.7778%** | every attack case |
| Containment rate — every compliant attempt refused before it executed | **5 of 5 — 100.0%** | compliant cases a control can contain |
| Task-damage rate — resisted, and answered wrongly anyway | **0 of 37 — 0.0%** | cases the agent resisted |

A1, Groq, `openai/gpt-oss-120b`, 2026-09-11, `runs/20260911-113246-1a97c9/ledger.jsonl`, over
the 45-case corpus. These three have different denominators and are never quoted as one
figure. **All five contained attempts were one shape** — `DROP TABLE audit_log`, refused by the
DDL/DML control before a connection opened — and no sandbox escape was attempted at all, so
100% is a thin claim. **Both wrong answers came from complying**, with an instruction no control
can contain. And **15 of the 45 cases were never seen**: A1 never looked inside the table that
carries the row-value injections. [docs/ATTACKS.md](docs/ATTACKS.md),
[results/attacks.json](results/attacks.json).

### Reserve set

Read once, after everything else is finished.

| Agent | Execution accuracy | Date | Ledger |
|---|---|---|---|
| TBD | TBD | TBD | TBD |

## Status

**Phases 1 to 4 are complete.** Both agents exist, both have been measured over the whole
150-task working set on the same model, and every failure of both has been read by hand. The
execution surface is behind **five tested controls** ([`docs/GUARDRAILS.md`](docs/GUARDRAILS.md),
each with a test and a stated limit), and A1 has been measured against a **45-case
prompt-injection corpus** ([`attacks/corpus.json`](attacks/corpus.json)) planted in table and
column names, column type metadata and row values — the three figures above. Every failure mode
the project has a number for is catalogued with its frequency, its denominator and the artifact
it came from, apart from the ones seen but never counted, in
[docs/FAILURES.md](docs/FAILURES.md). No number in this README comes from anything but a
committed ledger.

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
A trajectory that ran out of turns or tool calls gets no repair: repair answers *"you replied,
and the reply was not a single valid statement"*, not *"you never replied"*, and rescuing the
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
timeout and caps still apply, and it reads a copy. [docs/MCP.md](docs/MCP.md) carries the
working configuration and what the dependency costs.

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

## Development

```
uv sync
uv run pytest
uv run ruff check .
```

The test suite makes no live API calls. It passes with no network and no keys present.

## Licence

MIT. See [LICENSE](LICENSE).
