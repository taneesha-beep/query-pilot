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
| A1 agent | TBD | TBD | TBD | TBD | TBD | TBD |
| A2 cascade | TBD | TBD | TBD | TBD | TBD | TBD |

**A query returning nothing scores 7 of 150 — 4.6667% — of this working set for free**,
because that many of its reference queries return no rows. That floor belongs beside the
accuracy figure above it rather than in a footnote. Total cost of the A0 run: 106,740
tokens and **$0.00** — both providers are free tiers, so the cost that matters is quota.
Full write-up in [docs/RESULTS.md](docs/RESULTS.md), machine-readable in
[results/a0-working.json](results/a0-working.json).

### Execution accuracy by difficulty

| Agent | easy | medium | hard | extra | Denominators |
|---|---|---|---|---|---|
| A0 single-shot | 91.6667% | 81.5385% | 76.0000% | 79.1667% | 36 / 65 / 25 / 24 |
| A1 agent | TBD | TBD | TBD | TBD | TBD |

### Trajectory

| Measure | A1 |
|---|---|
| Tool calls per task (mean / median / p90) | TBD |
| Turns to solve | TBD |
| Recovery rate | TBD |
| Wasted-call rate | TBD |

A0 has no row here. A single-shot agent has no tool calls, no turns and no recovery, which
is the point of measuring them.

### Containment

| Measure | Value | Denominator |
|---|---|---|
| Compliance rate | TBD | TBD |
| Containment rate | TBD | TBD |
| Task-damage rate | TBD | TBD |

These three have different denominators and are never quoted as one figure.

### Reserve set

Read once, after everything else is finished.

| Agent | Execution accuracy | Date | Ledger |
|---|---|---|---|
| TBD | TBD | TBD | TBD |

## Status

**Phases 1 and 2 are complete; Phase 3 is open.** A0 exists, has been measured over the
whole working set, and every one of its failures has been read by hand. A1 — the agent with
tools, a loop and repair — does not exist yet, and no row above that names it will carry a
number until it does.

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
pushes the number **down**, which makes execution accuracy here a **lower bound**.

**And that lower bound has now been measured rather than asserted.** All 26 non-solves of
the A0 run were read by hand under a protocol fixed before the run started, in
[docs/FAILURES.md](docs/FAILURES.md). **Nine of the 26 are the reference query rather than
the model** — five of them returning demonstrably wrong data, each verified by running a
query, including two that compare a `TEXT` horsepower column against `150` and so count a
90-horsepower car as over 150. Nine more are the rule's own documented costs. Eight are the
model getting the data wrong, and none of the 26 was a malformed query or a wrong join.
**Nothing was re-scored**: 124 of 150 stands as taken, and what changes is how it is read.

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
