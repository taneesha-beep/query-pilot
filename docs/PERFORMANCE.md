# Performance

**This is a measurement of the scheduler, not of the providers' tiers.** Groq's free tier
allows 30 requests and 8,000 tokens a minute, and 1,000 requests and 200,000 tokens a day, per
pool and per model (`docs/PROVIDERS.md`). Those numbers are the provider's. What this document
measures is how close this project's scheduler — `src/query_pilot/client/scheduler.py` and the
buckets it keeps in `client/buckets.py` — came to the most those limits allow, and where the
rest of the time went. A faster scheduler would not buy a single request the limits forbid; a
slower one would waste ones they allow.

## 6.1 — Scheduler efficiency: how it is read

**Written before any ratio for A1 or A2-cheap was computed**, in a `docs:` commit of its own, the
way the cascade's reading was (`29464c1`). What had been seen before it was written is stated at
the end of this section.

### Which runs

Every full working-set run this project has taken, read from its ledger. Nothing is re-run and
no quota is spent.

| Run | Agent | Provider and model | Date | Ledger |
|---|---|---|---|---|
| `20260910-024454-1f69bc` | A1 | Groq, `openai/gpt-oss-120b` | 2026-09-10 | `runs/20260910-024454-1f69bc/ledger.jsonl` |
| `20260908-133316-faecd5` | A0 | Groq, `openai/gpt-oss-120b` | 2026-09-08 | `runs/20260908-133316-faecd5/ledger.jsonl` |
| `20260912-055938-9712c8` | A2-cheap | Groq, `openai/gpt-oss-20b` | 2026-09-12 | `runs/20260912-055938-9712c8/ledger.jsonl` |

**The headline is A1's**, fixed here before its ratio exists: it is the loop this project is
about, and the run where the scheduler met real pressure — several requests a task, refusals
at both scopes, a pool added mid-run. A0 and A2-cheap stand beside it in the same table.

Every run is one provider and one model. The roadmap's phrase "the declared quotas across
providers" does not apply: no run spilled over (`strong` has no spillover, and Phase 5 ran on
`cheap-no-spillover`), so the ceiling counts Groq pools of the model that served.

### The ceiling

**For each session of a run, the ceiling is the least time in which any scheduler admitting
requests the way the declared buckets do could have served that session's demand.**

- **Demand** is what the session actually sent and was answered: its attempt rows, their
  count, and their recorded tokens (prompt + completion), on the pools that served them. It is
  a property of the agent, not of the scheduler.
- **The buckets** are the declared limits in `config/providers.toml` — per pool, per model:
  RPM 30, RPD 1,000, TPM 8,000, TPD 200,000 — each refilling continuously at limit ÷ period,
  and **starting full at the start of every session**, because that is what the client's
  buckets do (constraint 74).
- **Admission** follows the client's own rule: a request goes out when every bucket on its pool
  is not in deficit, and its tokens are charged when its answer arrives. So each pool's **last**
  request in the session may leave its bucket in deficit, and its tokens never need refilling.
- **Across pools** the ceiling is a fluid bound: capacity may be spread over the pools present
  in any proportion. A pool is present in a session if it served an attempt or was refused in
  it; under this scheduler a present pool is always tried once the first one is in deficit, so
  a pool that never appears was not there.
- **A day-scope refusal is the provider's statement of its own counter, and the ceiling takes
  it.** From the moment Groq refuses a pool for the day, that pool's daily token bucket stands
  at what the refusal says is left — `Limit − Used` from its body — and refills at 200,000 per
  86,400 s from there. A minute-scope refusal changes nothing in the ceiling: it is the client
  sending a request the provider would not take, which is the scheduler's loss.

For each bucket kind, the least time at which the pools together could have admitted the
session's demand — requests for the request buckets; tokens less each pool's last request's
tokens for the token buckets — and **the ceiling is the latest of these, plus the latency of
the session's last answered request.** A run's ceiling is the sum over its sessions.

**Why not a steady rate.** "Tasks per minute from the quotas alone" read literally is
`pools × min(RPM ÷ requests a task, TPM ÷ tokens a task)`. That rate ignores the full bucket
every session starts with, so a session can beat it — and a ceiling a run can beat is not a
ceiling. It is reported beside the ratio, labelled, never as the denominator.

**Why not the daily horizon.** Both daily limits refill (constraint 95), so over a day the
sustainable rate is limit ÷ 1,440 a minute. It cannot anchor the ceiling for two reasons. The
client restarts its daily buckets full every session (constraint 74), so the scheduler never
acted on a day's view. And **Groq's day counter did not track recorded tokens on every pool**:
on 3.6, `groq#2` served 363,706 recorded tokens in the 2,821 s from its first attempt to the
run's end with no day-scope refusal, while a 200,000 bucket refilling at 200,000 a day admits at
most 206,531 in that span — 1.7610×. Yet between two consecutive day-scope refusals on `groq#1`,
Groq's `Used` moved with recorded tokens to within 69 and 5 tokens. Why the two differ is `TBD`.
Measured 2026-09-12 from `runs/20260910-024454-1f69bc/ledger.jsonl` and `runs/quota-walls.jsonl`.

**Concurrency 1 is not in the ceiling.** 6.1 asks for the ceiling from the quotas alone, and
concurrency is a run declaration, not a quota. Every run here declared 1, and for A1 and A2-cheap
it is **forced, not chosen** (constraints 46, 64, 91): a second attempt in flight can put the
per-minute token bucket into a deficit the client will not wait out. Its cost appears as a
loss, named as such, and beside the ceiling stands the concurrency-1 floor — the provider
latency and recorded local work no single-file scheduler can overlap.

### The time base

**Running time**: the sum over a run's sessions of `run_end.elapsed_s`, which equals the sum of
`ended_at − started_at` (checked). **Operator gaps** — wall clock between one session's end and
the next session's start — are not in the ratio and are reported beside it. A day-scope refusal
on the last open pool ends the session (the wait exceeds the client's 60 s `wait_ceiling_s`), so
waiting out a daily wall falls in a gap, not in running time.

**The ratio** is the run's ceiling ÷ its running time. **Achieved tasks per minute** is tasks
completed ÷ running minutes, and the **ceiling in tasks per minute** is the same tasks ÷ ceiling
minutes, so the ratio is one number read either way.

**Counted apart, never in the ratio:** operator gaps; capacity the provider withdrew with a
day-scope refusal (already out of the ceiling); and **repeated work** — the requests, tokens and
seconds of a trajectory cut off at a session's end and retried in the next. That demand was
served, so it is in the ceiling, and it is reported beside the ratio rather than hidden in it.

### Where the running time went — every second, once

Each session's running time is split along the ledger's own timestamps. An answered request
went out at `recorded_at − latency_s`; its answer arrived at `recorded_at`; a task began at its
row's `recorded_at − elapsed_s`. Every second of the session falls in exactly one of these, and
they sum to the running time by construction:

1. **Provider latency** — `latency_s` of every answered request.
2. **Waiting held by the declared buckets** — the part of a gap before a request that the
   replay below says a bucket was holding it, named by which one.
3. **Waiting across a refusal** — a gap in which the provider refused a request with a 429
   before this one was answered; its minute- or day-scope wall is recorded in
   `runs/quota-walls.jsonl`.
4. **Recorded local work** — tool calls that record their own `elapsed_s` in the transcript
   (`execute_sql` and `sample_rows`; `list_tables` and `describe_table` record none).
5. **Not held** — the rest of a gap the replay says no bucket was holding: unrecorded local
   work (building the prompt, reading a schema, writing the transcript), or waiting the replay
   cannot see.
6. **After a task's last answer** — validation, scoring the answer against the reference, and
   any request Groq refused with a 400 (a refused generation has no attempt row).
7. **Outside any task** — the run loop between tasks, and the start and end of a session.

### The queue wait — a derivation, not a record

**No file records how long a request waited.** `AttemptRow` has `recorded_at` and `latency_s`
and no wait, and the scheduler's sleeps — in `_wait_for_one` and in retry backoff — reach no
ledger. So the queue wait is **derived**, and every figure built on it says so.

**The replay.** Each session is replayed through the client's own `ModelBuckets`, on a clock
set to the ledger's timestamps: fresh buckets per pool at the session's start, a request charged
when it went out, its tokens charged when its answer arrived, and each refusal's block applied
when it was observed. At the moment each request went out, the pool it went to either had just
opened — its replayed `ready_at` within a tolerance of the send — or had been open for longer.

- **Held:** the pool opened within the tolerance of the send, after the gap began. The request
  was waiting for that bucket, and **its wait is the gap less recorded local work — an upper
  bound**, because the unrecorded local work before the wait began cannot be separated out.
- **Not held:** the pool had been open for longer. The scheduler sends the moment a pool is
  ready, so the whole gap was work, not waiting — as far as the replay can see.
- **Across a refusal:** a 429 fell inside the gap. The wait is the gap less recorded local work,
  and it includes the refused request's own round trip.

**The tolerance is 0.05 s.** The machine that took every run slept 300 times for 20 ms on
2026-09-12 and overshot by at most 1.53 ms (median 1.12 ms); 0.05 s is about thirty times that,
to cover the ledger stamping an attempt after the answer has been parsed and handed back up
through the agent. How many classifications would change at 0.01 s and at 0.2 s is reported
beside the distribution, so the choice is visible.

**What the replay cannot see.** The client also lowers its buckets to the provider's own
remaining counts (`ModelBuckets.observe`), and the ledger does not record those headers. A
request held by such a correction reads as **not held**. That is why category 5 is named "work,
or waiting the replay cannot see", and why its distribution is reported whole.

**Reported:** per run, over every answered request — count, how many waited zero, the 50th,
90th and 99th percentiles, the largest, and the total — and the total by what held it.

### Where the loss went

**The loss is running time less the ceiling.** It is split with the same replay. A declared
token bucket refills whether or not it is used, and refill is lost only when the bucket is
already full. So **the loss is the time a pool's per-minute token bucket sat full while the
ceiling was counting on its refill**, charged to whichever of the seven categories the session
was in at that moment, plus two terms at the session's end: capacity left unused when its last
request went out, and the time after its last answer. For one pool with the per-minute bucket
binding throughout, this identity is exact. Where it is not — pools joining, a day-scope refusal
cutting a pool's refill — **the difference from running time less the ceiling is stated**, not
spread over the categories.

Mapped to 6.1's three names:

- **Bucket starvation** — waiting for a declared bucket to refill — **is the ceiling, not a
  loss.** It is category 2, and it is what the limits cost.
- **Concurrency cap** and **provider latency** — a full bucket while a request was in flight
  (category 1) or while local work ran (categories 4–6): with only one request in flight, the
  refill had nowhere to go.
- Beyond 6.1's three: **the client's model disagreeing with Groq's** — a full bucket while a
  pool sat shut after a minute-scope refusal (category 3), which is the scheduler sending a
  request its own buckets allowed and Groq did not; and **the run loop** (category 7).

### Committed, and how CI checks it

`results/scheduler-efficiency.json` carries, per run and session, the inputs the reading needs
— every answered request's send time, latency, tokens and pool; every task's span; every
refusal in the session's window, with its body's `Used` where it has one; the recorded local
work — and every figure computed from them. **The ledgers are gitignored, so CI re-runs the
ceiling, the replay, the split of running time and the loss from those committed inputs and
requires the committed file back exactly.** The code is `src/query_pilot/run/throughput.py`;
`scripts/scheduler_efficiency.py` reads the ledgers and transcripts and writes the file.

### What had been seen before this was written

A read-only look at the three ledgers, taken while the plan for 6.1 was being made, computed
running time, wall clock and summed latency for all three, and A0's floor three ways — **800.5
s** at the steady rate, **740.5 s** from a full bucket, **733.5 s** with the last request's
tokens forgiven — against **737.4 s** running. So A0 was seen to sit within about half a
percent of its tight floor, and to beat the full-bucket one by 3.1 s. That is part of why the
headline was fixed on A1, whose ratio had not been computed. The same look found that A0's
session met **two minute-scope TPM refusals** (13:33:41Z and 13:34:02Z, `groq#1`, each blocking
the pool 1 s) where `docs/PROVIDERS.md` records "zero 429s".

### Results

`TBD` — 6.1's commit fills this section from `results/scheduler-efficiency.json`.
