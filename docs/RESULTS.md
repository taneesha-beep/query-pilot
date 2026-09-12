# Results

Every measured figure in this project, with the provider, the model string, the date and
the ledger file it came from. **A number without those four is not a result**, so nothing
appears here without them.

The machine-readable form of each run is in [`results/`](../results), projected from the
run's ledger and regenerable from it. The ledgers themselves are gitignored — only derived
summaries are committed — so a figure here traces to a ledger by name rather than by link.

---

## A0 on the working set — 2.4

**Single-shot: one prompt carrying the whole live schema, one call, one statement, no
tools, no repair and no second chance.** The agent is
[`src/query_pilot/agents/a0.py`](../src/query_pilot/agents/a0.py); what counts as the same
answer is [the equivalence rule](EQUIVALENCE.md), written and committed before a single
query was generated.

| | |
|---|---|
| Agent | A0, `strong` role |
| Provider | Groq |
| Model | `openai/gpt-oss-120b` (served: `openai/gpt-oss-120b`) |
| Date | 2026-09-08 |
| Run | `20260908-133316-faecd5` |
| Ledger | `runs/20260908-133316-faecd5/ledger.jsonl` |
| Projection | [`results/a0-working.json`](../results/a0-working.json) |
| Split | `splits/working.json` — 150 tasks, 20 databases |
| Declaration | [`config/runs/working-set.toml`](../config/runs/working-set.toml) |

### Execution accuracy

> **124 of 150 — 82.6667%.**
>
> **A query that returns nothing scores 7 of 150 — 4.6667% — of this split by doing
> nothing at all**, because 7 of its reference queries return no rows and an empty result
> matching an empty reference is a solve.

The floor belongs beside the figure rather than in a footnote, and
[`docs/EQUIVALENCE.md`](EQUIVALENCE.md) says so; this is the first number this project
reports and so the first place that is either kept or quietly broken. The floor over the
whole 1,034-task frame is 49 tasks, **4.7389%**, which is where the rule's own text records
it; the 7 above is the same measurement taken over the 150 tasks this figure is a rate on,
executed through the same sandbox that ran the candidates.

**Everything the rule does pushes 82.6667% down**, and every reason is documented rather than
discovered: the rule compares multisets rather than sets, columns positionally, and refuses a
truncated result outright; the sandbox's 30-second deadline rejects correct-but-slow
reformulations; and ties in an ordered reference are not repairable. Every one of those pushes
the number down. **Reference queries that are themselves wrong push it both ways**: 9 of A0's 26
failures are the reference, and at least 8 of its 124 solves are wrong answers a wrong
reference agrees with — so the figure is agreement with the references, not a floor on correct
answers. [docs/FAILURES.md](FAILURES.md) carries both counts. *This paragraph opened "82.6667%
is a lower bound" until 2026-09-12, when 4.4 found the second direction.*

### By difficulty

Labels are this project's own, differential-tested against canonical `eval_hardness` on all
1,034 frame tasks with zero disagreements.

| Difficulty | Solved | Of | |
|---|---|---|---|
| easy | 33 | 36 | 91.6667% |
| medium | 53 | 65 | 81.5385% |
| hard | 19 | 25 | 76.0000% |
| extra | 19 | 24 | 79.1667% |

### What the 26 non-solves were

Reason slugs from the equivalence rule, which may be added to but never renamed underneath
a committed result. These are the rule's verdicts, not a diagnosis — reading 26 failures by
hand and saying *why* the queries differed is [2.5](FAILURES.md).

| Reason | Count | |
|---|---|---|
| `value_mismatch` | 13 | Right shape, wrong values. |
| `row_count` | 11 | A different number of rows came back. |
| `column_count` | 2 | A different number of columns came back. |
| `no_sql` | 0 | Every one of 150 answers held a statement the extractor found. |
| `candidate_error` | 0 | Every statement executed. |
| `truncated` | 0 | No candidate result reached either cap. |
| `reference_error` | 0 | As the frame guarantees. |

**Three zeros worth naming.** No answer was unparseable, no generated statement failed to
execute, and no result was decided by a cap. The first is evidence about the prompt, the
second about the model, and the third is the requirement `docs/EQUIVALENCE.md` sets for a
cap — a cap that can decide a legitimate answer silently moves the accuracy figure, and
this one decided nothing.

### Cost

| | |
|---|---|
| Prompt tokens | 79,099 |
| Completion tokens | 27,641 |
| **Total** | **106,740** |
| Per declared task | 711.6 |
| **Per solved task** | **860.8** |
| **Money** | **$0.00** |

Tokens are counted from **every** attempt, answered or refused, because that is what was
spent. Here the two are the same number: 150 attempts for 150 tasks, none refused, none
retried. The money figure is zero and saying so is the correct way to report it — both
providers are free tiers and no paid API spend is permitted anywhere in this project, so
the cost that matters is quota, which is why it is denominated in tokens.

For scale: 106,740 tokens is **7.116% of the run's declared token ceiling** of 1,500,000,
and roughly half of Groq's published 200,000 tokens a day for one model. The ceiling was
declared from a hypothetical 10,000 tokens a task before any per-task cost was measured;
this run is the measurement, and it came in **14 times below** that hypothetical.

### The run itself

| | |
|---|---|
| Tasks complete / failed | 150 / 0 |
| Attempts | 150 — exactly 1.00 per task |
| Quota walls | 0 |
| Sessions | 1 — no resume, no quota reset spanned |
| Elapsed | 737.4 s (12.3 minutes), **5.121%** of the 14,400 s ceiling |
| Throughput | 12.21 tasks a minute |
| Concurrency | 1 |
| Provider latency | 0.483 s min, 0.984 s mean, 4.817 s max |
| Completions stopped at the output ceiling | 0 of 150 |

**Concurrency is 1 and that is a measured decision, not a default.** In-flight requests are
charged to the token buckets only when their answers arrive, so `concurrency` requests can
burst before any is paid for; charged together they put the per-minute token bucket into a
deficit of roughly `concurrency × tokens-per-attempt`, and a deficit deeper than
`wait_ceiling_s` of refill makes the client raise `AllPoolsExhausted` — which ends the run
as `pools_exhausted` **without a provider having refused anything.** Simulated over these
same 150 tasks with their measured prompt sizes, the declared concurrency of 8 ended the
run at task 91 of 150. The derivation is in the run declaration, the sizes it rests on are
in [`docs/a0-prompt-sizes.json`](a0-prompt-sizes.json), and a test holds the arithmetic.

**No completion stopped at the 1,024-token output ceiling**, so no answer was cut off
mid-statement — the largest completion in 2.3's acceptance run was 315 tokens and nothing
here reached the limit either.

### What this figure is, and is not

- It is **one draw**. A0 sends no temperature, so it runs at the provider's default and a
  second run of the same 150 tasks would not return the same 124. Nothing here is averaged
  over repeats, and the figure should be read with that in mind.
- It is **not the reserve number**. `splits/reserve.json` has never been read, by this run
  or any other, and is read once at the end of the project.
- It **includes the 10 tasks of 2.3's acceptance run**, which are members of the working
  set. Dropping them would report a rate over 140 tasks while calling it the working set.
  Nothing in A0 — its prompt, its extraction, its schema rendering — or in the equivalence
  rule changed between that acceptance run and this one; `git log` over
  `src/query_pilot/agents/` and `src/query_pilot/equivalence.py` since `9657094` is empty.
- It is the **baseline**, and it was built to be a good one. A1's gain in 3.6 is measured
  against this, on the same model, so that the comparison is about the agent loop and not
  about the model underneath it.

---

## A1 on the working set — 3.6

**The agent loop: four tools, no schema in the prompt, up to fourteen turns of discovery, one
repair attempt on a reply that fails validation.** The agent is
[`src/query_pilot/agents/a1.py`](../src/query_pilot/agents/a1.py). It is scored by the same
[equivalence rule](EQUIVALENCE.md), through the same sandbox, against the same 150 tasks and
the same references as A0.

| | |
|---|---|
| Agent | A1, `strong` role |
| Provider | Groq |
| Model | `openai/gpt-oss-120b` (served: `openai/gpt-oss-120b`) |
| Date | 2026-09-10 |
| Run | `20260910-024454-1f69bc` |
| Ledger | `runs/20260910-024454-1f69bc/ledger.jsonl` |
| Projection | [`results/a1-working.json`](../results/a1-working.json) |
| Trajectory metrics | [`results/a1-trajectory-metrics.json`](../results/a1-trajectory-metrics.json) |
| Split | `splits/working.json` — 150 tasks, 20 databases |
| Declaration | [`config/runs/a1-working.toml`](../config/runs/a1-working.toml) |

### Execution accuracy

> **116 of 150 — 77.3333%.**
>
> **A0, on the same 150 tasks and the same model, scored 124 of 150 — 82.6667%.**
>
> **The loop lost 8 tasks and cost 664,288 more tokens.** It is reported here the way it
> landed.

### By difficulty

| Difficulty | Solved | Of | | A0 |
|---|---|---|---|---|
| easy | 30 | 36 | 83.3333% | 91.6667% |
| medium | 49 | 65 | 75.3846% | 81.5385% |
| hard | 19 | 25 | 76.0000% | 76.0000% |
| extra | 18 | 24 | 75.0000% | 79.1667% |

### What the 34 non-solves were

| Reason | Count | |
|---|---|---|
| `value_mismatch` | 14 | Right shape, wrong values. |
| `row_count` | 10 | A different number of rows came back. |
| **`no_sql`** | **8** | **The final reply held no statement. A0 produced none of these.** |
| `column_count` | 2 | A different number of columns came back. |
| `candidate_error` | 0 | Every statement A1 committed to executed. |
| `truncated` | 0 | No candidate result reached either cap. |
| `reference_error` | 0 | As the frame guarantees. |

**All 8 `no_sql` are the same thing**, and `validation_rule` says so rather than leaving a
reader to group by `reason` alone: **`no_statement` × 8**, every one of them on a trajectory
that terminated at **`tool_call_limit`**. Not one is a malformed statement, a multi-statement
reply, or prose. A1 did not write bad SQL in these; it wrote **none**, because it ran out of
tool calls while still looking.

### Cost

| | | A0 |
|---|---|---|
| Prompt tokens | 692,516 | 79,099 |
| Completion tokens | 78,512 | 27,641 |
| **Total** | **771,028** | 106,740 |
| Per declared task | 5,140.2 | 711.6 |
| **Per solved task** | **6,646.8** | 860.8 |
| **Money** | **$0.00** | $0.00 |

771,028 tokens is **51.402%** of the run's declared ceiling of 1,500,000, against A0's
7.116%. The money figure is zero for the same reason it was for A0: both providers are free
tiers, no paid API spend is permitted anywhere in this project, and the cost that matters is
quota.

### The run itself

| | |
|---|---|
| Tasks complete / failed | 150 / 0 |
| Attempts | 827 — **5.51 per task**, against A0's exactly 1.00 |
| Sessions | **6**, spanning 144.2 minutes wall clock and **90.3 minutes running** |
| Longest session | 1,474.6 s — **10.24%** of the 14,400 s per-session ceiling |
| Quota walls | **12** — 4 day-scope on `groq#1`, 8 minute-scope on `groq#2` |
| Credential pools | `groq#1` 439 attempts, `groq#2` 388 |
| Concurrency | 1 — forced, not chosen; see the declaration |
| Provider latency | 0.222 s min, 0.984 s mean, 6.202 s max |
| Completions stopped at the output ceiling | **0 of 827** |
| Candidate results truncated or timed out | 0, 0 |

**This run met Groq's daily token ceiling and is the first thing in this project that has.**
It is written up in [docs/PROVIDERS.md](PROVIDERS.md). Six sessions is what a resumable run
looks like: a task recorded `complete` was never re-run, three tasks failed at a wall and were
retried, and the final ledger records 150 of 150 complete with 0 failed.

**Two credential pools, and A0 ran on one.** `GROQ_API_KEY_2` was added mid-run from a
separate Groq account, whose independence was established by behaviour before it was used —
40 concurrent requests saturating pool one, a control request **confirming pool one was still
refusing**, and pool two answering inside that window. Same provider, same pinned model
string, so this changes which credential the same model was reached through and nothing about
what it answered.

### The four trajectory metrics

Definitions are frozen and travel inside the metrics file itself. Read them there rather than
inferring them from the names.

| | mean | median | p90 | min | max | n |
|---|---|---|---|---|---|---|
| **Tool calls per task** | 4.3467 | 4 | 7 | 2 | 12 | 150 |
| **Turns to solve** | 4.819 | 5 | 6 | 3 | 13 | 116 |

**Wasted-call rate: 17 of 556 — 3.0576%**, over the 142 trajectories that produced a final
statement, 0.1197 wasted calls a task. The 8 with no final statement are excluded and counted
separately rather than folded in. **This is an upper bound on waste, not a measurement of
it** — a `describe_table` on a table the final query never names counts as wasted even where
it was the call that ruled that table out.

**Repairs: 1 attempt, 1 success, 0 blocked**, in 150 trajectories. 3.3's repair path fired
once in the whole measured run and worked.

#### Recovery rate — three denominators, and the headline one is empty

> **`error`: TBD over a denominator of 0.**
>
> **Not one `execute_sql` call in 150 trajectories returned an error.**

| Denominator | Of | Recovered | Rate |
|---|---|---|---|
| First `execute_sql` **errored** | **0** | 0 | **TBD** |
| First `execute_sql` returned **empty** | 6 | 1 | 16.6667% |
| Either (the roadmap's literal union) | 6 | 1 | 16.6667% |

Reported as counts beside them, because a first-call rule cannot see recovery that happens
later: **`trajectories_with_any_execute_sql_error` — 0**;
`trajectories_with_any_execute_sql_empty` — 9. And **24 of 150 trajectories ran no
`execute_sql` at all**; they are in none of the three denominators. First calls split
**120 rows · 6 empty · 24 none**.

**`TBD` over 0 is the honest answer and it was decided before the run**, in writing, precisely
so that a thin denominator could not be repaired afterwards by widening the definition.
Pooling `empty` into `error` would manufacture a denominator of 6 out of a denominator of 0,
and `empty` is reported apart for a reason this split makes concrete: an empty result **can be
the correct answer** here, for 7 of these 150 tasks.

**The zero is itself the finding.** **652 tool calls across the 150 trajectories — 150
`list_tables`, 243 `describe_table`, 193 `execute_sql`, 66 `sample_rows` — and every one of
those 193 statements ran**, against 20 real schemas the model had never been shown. *This
sentence said 676 and 198 until 2026-09-12: those count the six trajectories that were cut off
mid-task and retried as well, where every other figure here reads only the trajectory that
stands. `docs/a1-failure-counts.json` holds both; the committed metrics file always said 652.* Recovery rate is the
number A0 structurally cannot produce, and on this substrate with this model there was almost
nothing to recover from. That is a fact about Spider and about `openai/gpt-oss-120b`, not a
defect in the metric — and it is why the interesting claim in this project moves to
containment in Phase 4.

---

## A0 against A1 — 3.6

**Same model, same 150 tasks, same rule. The loop lost.**

| | A0 — single-shot | A1 — agent loop | Difference |
|---|---|---|---|
| **Execution accuracy** | **124 / 150 — 82.6667%** | **116 / 150 — 77.3333%** | **−8 tasks, −5.3333 points** |
| **Total tokens** | 106,740 | 771,028 | **+664,288 — 7.22×** |
| Tokens per declared task | 711.6 | 5,140.2 | 7.22× |
| **Tokens per solved task** | **860.8** | **6,646.8** | **7.72×** |
| Money | $0.00 | $0.00 | — |
| Provider requests | 150 — 1.00 a task | 827 — 5.51 a task | 5.51× |
| Requests per solved task | 1.21 | 7.13 | 5.89× |
| `no_sql` non-solves | 0 | 8 | +8 |
| Running time | 12.3 min | 90.3 min | 7.3× |
| Sessions | 1 | 6 | — |

**The token cost of the difference, stated as the item requires: 664,288 additional tokens
bought −8 solved tasks.** There is no cost per additional solved task, because there are no
additional solved tasks. Writing that ratio as a number would produce a negative figure
dressed as a price, and it is not one.

### What is held constant, and what is not

**Held constant, deliberately:** the model (`openai/gpt-oss-120b`) and the role (`strong`);
the output ceiling (`MAX_OUTPUT_TOKENS` 1,024); the answer rules, which are one shared string
and not two copies; the sandbox and the four controls both runs went through (4.1 later added
two more, which no committed run has been measured under); the equivalence rule and its eight
reason slugs, all committed before either run; the same 150 tasks in `splits/working.json`;
concurrency 1; and no temperature sent by either agent, so both ran at the provider's default.

**Deliberately not held constant — these are what "the loop" means:**

1. **A0 is given the whole live schema in its prompt. A1 is given none and must discover it.**
2. **A0 gets one request and lives with the answer. A1 gets up to 14 turns and 12 tool calls.**
3. **A1 validates its final reply and repairs it once on failure. A0 does neither.**
4. **A1 can read row values through `sample_rows` and test SQL through `execute_sql`. A0 can
   read neither.** The prompt holds no row values by design.

One further asymmetry that is **not** a design decision and is recorded rather than defended:
A0 ran on one credential pool in one session, A1 on two pools across six. Same provider, same
pinned model, and every attempt row records which pool served it.

### Where the eight tasks went

Both agents were scored on the same 150 tasks. **Both solved 114. A0 alone solved 10, A1 alone
solved 2, and 24 defeated both.**

**Seven of A1's ten losses are one database.** On `flight_2`, **A0 solved 11 of 11 and A1
solved 4 of 11.** Every other database is within one task, in either direction.

**And five of the ten losses are the tasks a query returning nothing already scores for
free.** This split has 7 such tasks — the floor reported beside every accuracy figure in this
document. **A0 collected all 7. A1 collected 2.**

| | A0 | A1 |
|---|---|---|
| The 7 empty-reference tasks | **7 / 7** | **2 / 7** |
| The other 143 tasks | 117 / 143 — 81.8182% | 114 / 143 — 79.7203% |

**So the deficit decomposes: 5 of the 8 are tasks scored by returning nothing, and 3 are
ordinary.** Excluding the free tasks entirely, A1 is behind by 3 tasks rather than 8.

#### One trajectory, read in full, because it is the whole result in miniature

**`dev-0186`, `flight_2`:** *"Give the airport code and airport name corresonding to the city
Anthony."* The reference is `WHERE city = "Anthony"`.

**The stored value is `'Anthony '` — with a trailing space.** Verified read-only: `City =
'Anthony'` returns **0 rows**, `City LIKE '%Anthony%'` returns exactly one, `('ANY',
'Anthony ')`, and `length(City)` is 8.

**A0 wrote `WHERE City = 'Anthony'`, returned nothing, matched the reference's nothing, and
scored a solve.** It could not see the trailing space and did not need to.

**A1 found it.** Its twelve tool calls, in order: `list_tables`, `describe_table airports`,
`sample_rows`, then `City = 'Anthony'` → 0 rows, `LIKE '%Anthony%'` → 1 row, the equality
again, another sample, a three-column `LIKE`, `SELECT *` with equality, a `lower()` comparison,
equality once more, and finally **`SELECT City, length(City) … LIKE '%Anthony%'`** — the call
that identifies the trailing space. Then it hit `TOOL_CALL_LIMIT` and its final reply was
**the empty string**. No statement, `no_sql`, task lost.

**A1 was punished for being right.** It disbelieved an empty result, investigated, found a
real defect in the stored data, and ran out of room to answer. A0 was rewarded for being
unable to look. **Five of the eight `no_sql` tasks are this shape** — `dev-0186`, `dev-0207`,
`dev-0238`, `dev-0254` and `dev-0256`, all `flight_2`, whose stored airport codes and cities are
padded with spaces. Counted when 4.4 read all 34 failures; this sentence said three until
2026-09-12, from a partial reading. And A0's "solve" here, and on every `flight_2` task it won
and A1 lost, is a wrong answer the defective reference agrees with — [docs/FAILURES.md](FAILURES.md).

### What this figure is, and is not

- **It is one draw.** Neither agent sends a temperature, so both run at the provider's default
  and a second run of these 150 tasks would not return the same numbers. Nothing here is
  averaged over repeats.
- **It is not the reserve number.** `splits/reserve.json` has never been read, by this run or
  any other.
- **The limits were not moved after seeing the result.** `TURN_LIMIT` 14, `TOOL_CALL_LIMIT` 12,
  `REPAIR_LIMIT` 1 and `PROMPT_CEILING_CHARS` 22,776 are exactly what the run was declared
  with. Eight tasks were lost to `TOOL_CALL_LIMIT` and raising it afterwards would be choosing
  the number, so the count is reported and the limit stands.
- **The roadmap predicted the shape of this and named the reason: Spider's schemas fit in a
  prompt.** They do — 4.5 tables and about 1,048 characters of DDL for the average working-set
  task. When the whole world fits in one prompt, discovery buys nothing and costs 7.22× in
  tokens and eight tasks in accuracy.
- **The two tasks A1 won were predicted in writing before it was measured.**
  [`docs/FAILURES.md`](FAILURES.md) categorised `dev-0404` and `dev-0549` as A0 writing a
  literal that does not match the stored spelling — `'math'` against `Math`, `'North Carolina'`
  against `'NorthCarolina'` — and said of the second: *the prompt holds no row values by
  design, so the spelling of a stored literal is not something A0 can see; 3.1's tools are
  where an agent gets to look.* A1 looked, and won exactly those two.
- **The interesting claim moves, as the roadmap said it should.** Recovery rate came back
  `TBD` over 0 because nothing errored. What A1 demonstrably has that A0 cannot is the ability
  to read the data before answering — worth two tasks here, and the thing Phase 4 measures
  under attack.

---

## A2 — the cascade — 5.2

**The cascade lost, and it is reported that way.** It solved **one task more** than
always-strong and paid **1.52× the tokens** to do it: **10,032.3 tokens a solved task against
A1's 6,646.8**. By the rule fixed in writing before its figures existed (below, committed in
`29464c1` while the cheap run stood at 105 of 150), that is a loss. The reason is not the
escalation rule, which works better on the cheap model than on the strong one; it is that **the
cheap model is not cheaper here.** It spent 5,632.4 tokens a task against the strong model's
5,140.2, and in this project a token is a token whichever model spends it.

### The three, and the one table

| Agent | What it is | Execution accuracy | Tokens | Tokens per solved task | Requests | Provider · model | Date | Ledger |
|---|---|---|---|---|---|---|---|---|
| **A2-cheap** — always-cheap | A1's loop on the cheap model | **89 / 150 — 59.3333%** | 844,865 | 9,492.9 | 837 | Groq · `openai/gpt-oss-20b` | 2026-09-12 | `runs/20260912-055938-9712c8/ledger.jsonl` |
| **A1** — always-strong | 3.6, as measured and frozen | **116 / 150 — 77.3333%** | 771,028 | **6,646.8** | 827 | Groq · `openai/gpt-oss-120b` | 2026-09-10 | `runs/20260910-024454-1f69bc/ledger.jsonl` |
| **A2** — the cascade | A2-cheap, escalated to A1 when the rule fires | **117 / 150 — 78.0%** | 1,173,781 | 10,032.3 | 1,160 | Groq · both models above | 2026-09-12 and 2026-09-10 | both ledgers above |

Tokens are recorded tokens, every attempt row, retried attempts included; requests are answered
requests (attempt rows). On the standing-trajectory basis the order is the same: A2-cheap
9,430.8, A1 6,457.0, A2 9,889.0 a solved task. Money: **$0.00** for all three. Files:
`results/a2-cheap-working.json`, `results/a1-working.json`, `results/a2-working.json`, and the
rule's decisions per task in `docs/escalation-a2-cheap-working.json`.

**On the frontier** (solved tasks against tokens): **A2-cheap is dominated by A1**, which solved
27 more tasks for 73,837 fewer tokens. Neither A1 nor A2 is dominated: A2 has the one extra
task, A1 has 402,753 fewer tokens. **The verdict is 5.2's metric, cost per
solved task, and A2 loses it** (`cascade_wins: false`, compared exactly).

**A2-cheap alone.** Groq, `openai/gpt-oss-20b`, 2026-09-12, run `20260912-055938-9712c8`, the
same 150 tasks and the same four limits as 3.6 — the model is the one thing that moved.
Terminations: `answer` 113, **`provider_rejected` 28**, `tool_call_limit` 9. Its 42 `no_sql`
are all "no statement": 28 refused, 9 out of tool calls, 5 that answered with a value and whose
one repair Groq refused. By difficulty: easy 27 of 36, medium 39 of 65, hard 14 of 25, extra 9
of 24. It ran no `execute_sql` on 42 trajectories; no first query errored, so recovery rate's
headline is `TBD` over 0 here too. 837 attempt rows over six sessions — four ended by a request
ceiling, one by the daily wall on both pools, the sixth complete — 54.3
minutes running across 05:59:39Z–12:41:35Z, on three Groq pools: 337 on `groq#1`, 311 on
`groq#2`, 189 on `groq#3`. **33 requests Groq refused over the model's own output have no attempt
row, and their tokens are in no figure here.**

**The rule on the cheap model** (`docs/escalation-a2-cheap-working.json`, the rule of `c1f8520`,
untuned): it escalates **46 of 150**, and **not one of the 46 had solved** — its precision holds.
It catches **46 of the 61 failures, 75.4098%**, against 26.4706% on the strong model: the cheap
model's failures announce themselves. Clauses: did not answer 37, validation failed 42, first
query errored 0, first query empty 8. **Running no `execute_sql` solved 9 of 42 here** (21.4%)
where it solved 21 of 24 on the strong model — the clause 5.1 rejected on 3.6's data would have
been a signal on this one. It was not added: the rule was frozen before this run (constraint 88).

**The cascade.** Composed from those two runs. **Where the rule was silent** — 104 tasks — the
cheap model solved 89, and the strong model had solved 88 of the same tasks. **Where it fired**
— 46 — the cheap model had solved none and the strong model solves 28. Against always-strong,
the cascade wins 4 tasks and loses 3 (`dev-0106`, `dev-0571`, `dev-0613`, `dev-0980` against
`dev-0331`, `dev-0483`, `dev-0496`), every one of them a task the rule left with the cheap model;
on the escalated 46 the two are the same trajectory by construction. **What the escalations cost:
281,088 cheap tokens spent on the 46 tasks before they were handed on, plus 328,916 strong
tokens — 21,785.9 tokens for each of the 28 solves they bought.**

### Why it lost, and what would flip it

- **The cheap model is not cheaper per task.** A1's loop on 20b spent 844,865 tokens on 150 tasks
  against 120b's 771,028 — more discovery calls (4.52 a task against 4.35), longer trajectories,
  and 28 refusals that end a trajectory after its tokens are spent. So the cascade starts behind
  always-strong before it escalates anything.
- **Every escalation pays twice.** The 46 escalated tasks had already cost 281,088 cheap tokens,
  a third of the cheap run, and bought nothing on the cheap side.
- **What would flip it: a price.** Every token here is one unit and costs $0.00. **The break-even
  price ratio is 0.5312** — if a cheap token cost less than 53.12% of a strong one, the cascade's
  cost per solved task would fall below always-strong's. On free tiers the useful reading is the
  split by model: **the cascade spent 328,916 strong-model tokens where always-strong spent
  771,028 — 442,112 fewer, 57.3406% of the scarce model's daily allowance left unspent** — for
  844,865 cheap-model tokens drawn from a separate allowance.
- **The one extra task is one draw.** Neither run has been repeated, and 117 against 116 is the
  net of four tasks won and three lost. No threshold for "near" was set (below), so it is
  reported as one task and not as "matching" always-strong.

**What follows was fixed in writing on 2026-09-12, while that run stood at 47 of 150, and was
committed before any cascade figure existed** (`29464c1`) — so that how the cascade is read
could not be chosen after seeing which way it lands. The same text travels as `DEFINITIONS` in
`results/a2-working.json`.

### How the cascade is built: composed, not run

A2 is A1's loop on the cheap model, handed to the strong model when 5.1's frozen rule fires on
the cheap trajectory ([ESCALATION.md](ESCALATION.md), frozen at `c1f8520`). **Each task takes the
always-cheap run's outcome when the rule is silent, and A1's measured outcome on the same task
— 3.6, run `20260910-024454-1f69bc` — when it fires.** An escalation restarts A1 on the strong
model from nothing, with the prompt, limits and model 3.6 ran, so 3.6's trajectory on that task
is a draw of exactly what a live escalation would make. What composing gives up is a draw taken
on the same day under the same rule for refused output; what it buys is **a paired comparison**
— on an escalated task the cascade and always-strong share one strong trajectory, so every
difference between them comes from the tasks the rule left with the cheap model — and zero
strong-model quota.

### What "cost" means

**Recorded tokens: prompt plus completion of every attempt row in a run's ledger, attributed to
its task by the row's `task_id`, retried attempts included.** That is the basis A1's committed
**6,646.8 tokens a solved task** already stands on (771,028 over 116), so always-strong's figure
is 3.6's, untouched. Always-cheap: every attempt row of its run. The cascade: every cheap row, all
150 tasks, plus 3.6's rows for the escalated tasks. It is exact rather than undercounted, because
every one of 3.6's 827 attempt rows carries its `task_id` and they sum to 771,028 — checked
2026-09-12. The 22,017 tokens that 3.6's per-task figures (749,011 in all) do not carry are five
retried tasks' earlier attempts: `dev-0044` 3,126, `dev-0254` 11,243, `dev-0496` 3,715,
`dev-0758` 3,299, `dev-0816` 634.

**Beside it, labelled, the standing-trajectory basis** — each task's final trajectory alone —
for all three. **Neither basis sees a request the provider refused**: it has no attempt row and
no ledger records its tokens (33 on the cheap run — 25 when this was first written at 105 of 150
— and 1 on 3.6's, which is not an escalated task).
They are counted beside every cost and never estimated.

### When the cascade wins

**It wins 5.2's metric if and only if its cost per solved task is strictly below
always-strong's 6,646.8; otherwise it loses.** Compared exactly, never on rounded figures.

**No threshold was set for "near strong-model accuracy".** Neither model has been run twice, so
there is no measured spread to derive a tolerance from. The cascade's difference from
always-strong is written as a number of tasks and a token ratio, with no adjective, and each of
the three is marked dominated or not on solved tasks against tokens.

**Two readings beside the verdict, not part of it**, fixed now because the premise is at risk:
in this project a 20b token and a 120b token are the same unit and both cost $0.00, and a cascade
is built on the cheap model's tokens being cheaper.

- **Tokens split by model.** Each model has its own daily allowance, so strong-model tokens
  saved is what a cascade buys on free tiers.
- **The break-even price ratio**, r* = (S × N_cascade / N_strong − S_escalated) / C: the price
  of a cheap token, relative to a strong one, below which the cascade's cost per solved task
  would fall under always-strong's. C is the cascade's cheap tokens, S_escalated its strong
  tokens, S and N_strong always-strong's tokens and solved tasks. r* ≤ 0 means no price does it.

### What will be declared beside the table, whichever way it lands

- **Rule for refused output.** 3.6 ran under `fail_task` and retried its one refusal
  (`dev-0758`); the cheap run scores such a refusal as `provider_rejected`, unsolved and
  escalated. A composed escalation of `dev-0758` would carry 3.6's retried outcome.
- **Dates and pools.** Always-strong ran on 2026-09-10 on two Groq pools. Always-cheap ran on
  2026-09-12 on **three**: after the daily wall the author added a third key from a third
  account, `groq#3`, and a separate daily counter was confirmed before it was used (999
  requests remaining on it against 802 on `groq#1`). It served 189 of the 837 attempt rows. A
  pool is an account, not a model: every row names `openai/gpt-oss-20b`. *(This line said
  "both used both Groq pools" when first committed.)*
- **The rule was calibrated inside the working set.** 5.1's preflight ran the 15 smoke tasks,
  which are working-set tasks, and the rule's one change after a preflight — `provider_rejected`
  in clause 1, by the author's decision before the freeze — came from them.
- **Recall is low by construction.** On the strong model the rule catches 9 of 34 failures; the
  cascade recovers only failures that announce themselves.
- **Accuracy is agreement with the references**, which err both ways. The seven tasks whose
  reference returns no rows, and the eight whose references 4.4 verified by query return wrong
  data, are listed with all three agents' outcomes in `results/a2-working.json` — among them, a
  cheap first query that comes back empty on an empty-reference task fires clause 4 and hands a
  free "solve" to the strong model. **The seven are found by running each reference through the
  sandbox, not read from a results file**: `reference_rows` in `results/a1-working.json` is blank
  wherever A1 wrote no SQL, because the comparison never ran, so that file shows 4 empty
  references where the split has 7.
  **What happened on them:** clause 4 fired on all seven, but the cheap model had "solved" none
  of them — so no free solve was handed away. A0 collected 7 of 7, A1 2 of 7, A2-cheap 0 of 7,
  and the cascade 2 of 7, both through the strong model (`dev-0237`, `dev-0909`). Six of the
  seven are `flight_2`. None of the three solved any of the eight verified wrong references, so
  those move no figure in this table.
- **Stopping and resuming costs tokens.** A task in flight when a run stops is retried from
  nothing, and its first attempt stays in the run's total; both runs spanned several stops.
- **The projection's token note** says "every attempt, answered or refused" — true of what has an
  attempt row, not of the refused requests above.
