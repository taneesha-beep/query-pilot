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
| A0 single-shot | TBD | TBD | TBD | TBD | TBD | TBD |
| A1 agent | TBD | TBD | TBD | TBD | TBD | TBD |
| A2 cascade | TBD | TBD | TBD | TBD | TBD | TBD |

### Execution accuracy by difficulty

| Agent | easy | medium | hard | extra | Denominators |
|---|---|---|---|---|---|
| A0 single-shot | TBD | TBD | TBD | TBD | TBD |
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

**Phase 1 is complete; Phase 2 is open.** No agent exists yet, and **no table above will
have a number in it until Phase 2 has one to put there.**

What exists is the infrastructure those measurements will run on: an async client over two
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
