# The escalation rule — 5.1

**When a cheap trajectory is handed to the strong model.** A2 is a cascade: A1's loop runs
first on the cheap model (`openai/gpt-oss-20b`), and this rule reads the trajectory that came
back and decides whether the task goes to the strong one (`openai/gpt-oss-120b`). The rule is
code — `src/query_pilot/agents/escalation.py` — and its definitions are written into every file
it produces. **It was committed before any 5.2 run spent anything, and it is frozen from that
commit:** tuning a clause after seeing which way the cascade lands is the failure this item
exists to prevent.

## The rule

Escalate when any of these holds of the cheap trajectory:

1. **`did_not_answer`** — it terminated on `turn_limit`, `tool_call_limit`, `prompt_ceiling` or
   `provider_rejected` rather than `answer`.
2. **`validation_failed`** — its final reply failed validation, after its one repair where one
   was made. Read as *the final validation failed, whatever the termination*, which is the
   reading the table below was computed with.
3. **`first_query_error`** — its first `execute_sql` came back `ok=false`.
4. **`first_query_empty`** — its first `execute_sql` came back with zero rows.

**One escalation at most.** The cheap trajectory's tokens count in the escalated task's cost,
and an escalated task the strong model also fails has paid for both.

**Not a clause: running no `execute_sql` at all.** On 3.6's run those trajectories solved 21 of
24 — above the average — so it is not a failure signal, and escalating on it would spend the
strong model on tasks the cheap one had already got right. It is counted beside the clauses so
the rejection stays visible.

**Only a complete trajectory is decided.** A failed task — the budget guard stopped it, or a
client error ended it — is retried by the run, never escalated.

**Where the inputs come from.** Clauses 1 and 2 read the ledger task row's `termination` and
`validation_rule`; clauses 3 and 4 read the transcript's last bracket through 3.4's own reader.
The two records are checked against each other and a disagreement raises.

## Measured on the strong model — 3.6's run

Groq, `openai/gpt-oss-120b`, 2026-09-10, run `20260910-024454-1f69bc`,
`runs/20260910-024454-1f69bc/ledger.jsonl`. Written to `docs/escalation-a1-working.json` by
`scripts/escalation_table.py`; the transcripts are not committed, so `tests/test_escalation.py`
checks every task's inputs against the frozen `results/a1-working.json` and
`results/a1-trajectory-metrics.json` and recomputes the table from them.

| | Fires | Solved | |
|---|---|---|---|
| **Would escalate** (any clause) | 10 of 150 | 1 | 10.0% |
| Would not escalate | 140 | 115 | 82.1429% |
| 1 — did not answer | 8 | 0 | |
| 2 — validation failed | 8 | 0 | |
| 3 — first query errored | 0 | 0 | `TBD` |
| 4 — first query empty | 6 | 1 | 16.6667% |
| *not a clause* — no `execute_sql` | 24 | 21 | 87.5% |

**Precision is high and recall is low.** Of the 34 trajectories that did not solve, the rule
sends 9 on — **26.4706% recall**. The cascade can only recover failures that announce
themselves, and 5.2's write-up says so rather than presenting it as a general repair. On this
run clauses 1 and 2 fire on the same eight trajectories — every `tool_call_limit` one, each
ending with no statement — and clause 4 fires alone on two.

## The preflight on the cheap model

Required before the rule was frozen, because every figure above is the strong model's and the
rule runs on the cheap one. **Calibrating on the smoke set means calibrating on fifteen tasks
that sit inside the working set** — an overlap 5.2's write-up declares. What the preflight was
allowed to change was fixed with the author before it spent anything: **none of the four
clauses' text**; it could stop Phase 5, find a defect in how an input is read, or change the
quota plan. Two stop conditions were fixed with it: a request over the endpoint's per-minute
budget, and a trajectory stopped by the prompt ceiling. **No per-task outcome is in its
committed file**, because a per-clause solve rate over fifteen trajectories is exactly what a
clause would be tuned against.

**Both preflight runs used a role with no spillover**, `cheap-no-spillover` in
`config/providers.toml`: `cheap` spills to a Google model whose tool-calling path A1 has never
exercised, and a trajectory finished there would be a third model under the cheap one's name.
Every attempt row of both runs names `groq openai/gpt-oss-20b`.

### The first preflight found a failure the rule could not see, and was superseded

Groq, `openai/gpt-oss-20b`, 2026-09-12, run `20260912-052224-4f8be8`,
`runs/20260912-052224-4f8be8/ledger.jsonl`, under 3.6's rule for a provider error — the task
fails and a resume retries it (constraint 76). 98 requests; 92 of them answered, 77,286 tokens.
**Groq refused 6 of the 19 trajectories not cut off by a request ceiling with an HTTP 400 over
the model's own output**: 4 of the 15 tasks on the first attempt, and 2 of those 4 again on
retry — `dev-0638` identically, the same malformed call after the same two turns. The four
clauses fired on **none** of the 13 trajectories that completed. So the cheap model's most
frequent failure was a failed task — retried, never decided — and the stop condition fixed for
exactly that case (a failure repeating on retry) tripped. The run was stopped at 13 of 15 and
is not resumed.

**The author decided, before the rule was committed, to score such a refusal** rather than
retry it. A run that declares `rejected_generation = "score_unsolved"` ends the trajectory as
**`provider_rejected`** when Groq answers HTTP 400 and hands the model's output back as
`failed_generation` — complete, unsolved, with no repair, and its paid turns counted. Nothing
weaker counts: a 400 about this project's own request carries no such field and still fails the
task, as a quota wall does. Clause 1 lists `provider_rejected`. **This is a change the
preflight's agreed scope did not allow, made by the author's explicit decision and recorded as
one**; it changes nothing on 3.6's run, which has no such trajectory, and 3.6 remains under the
old rule — it retried its one refusal. That asymmetry is declared in 5.2 rather than hidden.

### The preflight the rule was frozen on

Groq, `openai/gpt-oss-20b`, 2026-09-12, run `20260912-054709-c34308`,
`runs/20260912-054709-c34308/ledger.jsonl`, lifted byte-for-byte to
`tests/transcripts/a2-cheap-smoke-lifted/`; `docs/escalation-a2-cheap-smoke.json` is regenerated
from the lift whole by `tests/test_escalation.py`. 81 requests, 77 answered (41 on `groq#1`, 36
on `groq#2`), 67,520 tokens; the other 4 were refused and have no attempt row. 15 of 15 tasks
complete, none failed.

| Clause | Fires, of 15 |
|---|---|
| **Would escalate** (any clause) | **5** |
| 1 — did not answer | 4 |
| 2 — validation failed | 5 |
| 3 — first query errored | 0 |
| 4 — first query empty | 0 |
| *not a clause* — no `execute_sql` | 4 |

Terminations: `answer` 11, `provider_rejected` 3, `tool_call_limit` 1. Clause 1's four are the
three refusals and the one `tool_call_limit` trajectory; clause 2 adds `dev-0700`, which
answered with no statement and whose repair Groq refused ("Tool choice is none, but model called
a tool"). Every trajectory that ran no query is one of those four refused or unanswered ones.
**The rule did not change after this run.**

**What 20b's loop looks like, for comparison with constraint 57.** 73 assistant messages: 58
with one tool call, 15 with none, none with more — one call a turn holds on the cheap model too.
The three refused trajectories came in two shapes, both Groq's `tool_use_failed`: a format
token leaked into the tool's name (`describe_table<|channel|>commentary`, twice) and arguments
that were not valid JSON. Five repairs were requested; four succeeded and one — `dev-0700`'s —
was refused, a third shape: a tool call on the repair turn, which offers no tools.

## Constraint 64's arithmetic, measured on A1's own requests

Every A1 run is forced to concurrency 1 by `PROMPT_CEILING_CHARS / 3.265 + MAX_OUTPUT_TOKENS` =
**8,000 tokens**, the endpoint's whole per-minute budget. **3.265 characters per token was
measured on A0's prompts**, which carry no tool schemas and no tool results. Measured the way
the ceiling counts — each request's replayed conversation against the prompt tokens the provider
reported for it — A1's own requests are denser:

| | 120b — 3.6's run | 20b — the preflight |
|---|---|---|
| Requests paired | 803 | 73 |
| Characters per prompt token, min / median / max | 1.879 / 2.066 / 3.323 | 1.914 / 2.022 / 3.292 |
| Fitted line, prompt tokens | 158.7 + chars / 2.650 | 166.6 + chars / 2.714 |
| **An attempt at the ceiling, by that line** | **9,777** | **9,582** |
| Largest request actually made | 3,426 tokens (42.83%) | 1,894 tokens (23.67%) |
| Longest conversation | 8,855 chars (38.88% of the ceiling) | 3,636 chars (15.96%) |
| Requests over 8,000 tokens | 0 | 0 |

**So the 8,000 is wrong at the ceiling on both models, by about a fifth, and for a reason that is
not the model.** Nothing measured came near it, and the two models' densities agree, as one
tokenizer predicts; the 20b line is extrapolated from conversations no longer than 16% of the
ceiling. **No limit was moved** — A1's four limits are frozen underneath 3.6 (constraint 72), and
concurrency 1 was already the only safe setting. What an overshoot would cost is a run stopped as
`pools_exhausted` and resumed, with the task in flight retried; it cannot move a figure.

## What this rule feeds

5.2's always-cheap run — A1's loop on the cheap model over the 150-task working set, declared in
`config/runs/a2-cheap-working.toml` with the same ceilings as 3.6's and committed with this rule
— started on 2026-09-12 as run `20260912-055938-9712c8`. Its figures, and the cascade built from
it and 3.6, belong to 5.2 and are reported there; nothing in this file will be revised to suit
them.
