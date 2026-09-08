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

**82.6667% is a lower bound**, and every reason is documented rather than discovered: the
rule compares multisets rather than sets, columns positionally, and refuses a truncated
result outright; the sandbox's 30-second deadline rejects correct-but-slow reformulations;
and ties in an ordered reference are not repairable. Every one of those pushes the number
down. Whether reference queries that are themselves wrong push it down further is
[2.5](FAILURES.md)'s question.

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
