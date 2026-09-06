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

Phase 0 of 7. Nothing is measured yet.

## Development

```
uv sync
uv run pytest
uv run ruff check .
```

The test suite makes no live API calls. It passes with no network and no keys present.

## Licence

MIT. See [LICENSE](LICENSE).
