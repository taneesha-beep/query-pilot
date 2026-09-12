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

Groq, three runs, every figure from `results/scheduler-efficiency.json` — written on 2026-09-12
by `scripts/scheduler_efficiency.py` from the ledgers named in the table at the top of this
section and `runs/quota-walls.jsonl`, and recomputed in CI from the inputs the file carries.

| Run | Agent | Model | Sessions | Running | Ceiling | **Ratio** | Tasks a minute, achieved / ceiling |
|---|---|---|---|---|---|---|---|
| `20260910-024454-1f69bc` | **A1** | `openai/gpt-oss-120b` | 6 | 5,418.2073 s | 5,244.1274 s | **96.7871%** | 1.6611 / 1.7162 |
| `20260908-133316-faecd5` | A0 | `openai/gpt-oss-120b` | 1 | 737.3566 s | 734.4157 s | 99.6012% | 12.2058 / 12.2546 |
| `20260912-055938-9712c8` | A2-cheap | `openai/gpt-oss-20b` | 6 | 3,255.4176 s | 3,084.1555 s | 94.7392% | 2.7646 / 2.9181 |

**The scheduler came within 3.2% of what the declared quotas allow on A1's loop, and the
quotas, not the scheduler, set the pace.** No run beat its ceiling, and a test fails if one ever
does. The loss in seconds — 174.0799 on A1, 2.9408 on A0, 171.2621 on A2-cheap — is taken apart
below, and **most of A1's is not the scheduler's**: 140.6357 s of it sits in the two sessions
where Groq had already refused `groq#1` for the day.

**Beside the ratio, as fixed in advance:**

- **The steady rate is beaten, as the definition said it could be.** A0 finished in 737.3566 s
  against a steady-rate time of 800.55 s — a steady ratio of 1.0857 — because it started with a
  full bucket. A1's steady ratio is 0.8103 and A2-cheap's 0.8621. None of the three is a ceiling.
- **Concurrency 1 was never close to binding.** The provider latency and recorded local work no
  one-at-a-time scheduler can overlap is 813.7624 s on A1 against a ceiling of 5,244.1274 s;
  147.525 s against 734.4157 s on A0; 582.7426 s against 3,084.1555 s on A2-cheap.
- **Operator gaps**, outside the ratio: 3,235.102 s on A1 (wall clock 8,653.3093 s) and
  20,861.7789 s on A2-cheap (24,117.1965 s); none on A0.
- **Repeated work**, inside the ceiling and reported apart: A1 re-ran 6 cut-off trajectories —
  24 requests, 22,017 tokens, 176.5965 s; A2-cheap 3 — 7 requests, 5,524 tokens, 27.6605 s; A0
  none. The token figures are the ones 5.2 reconciled against the projections.
- **Refusals inside running time:** A1 8 minute-scope and 4 day-scope; A0 **2 minute-scope**,
  not the zero `docs/PROVIDERS.md` recorded; A2-cheap 80 and 4.

#### Where the running time went

| Seconds | A1 | A0 | A2-cheap |
|---|---|---|---|
| Provider latency | 813.4477 | 147.525 | 582.3277 |
| Waiting held by the declared buckets | 0.0 | 0.0 | 356.4385 |
| Waiting across a refusal | 32.6657 | 10.6516 | 138.0773 |
| Recorded local work | 0.3147 | 0.0 | 0.4149 |
| Not held — work, or waiting the replay cannot see | 4,557.751 | 578.5586 | 2,074.8588 |
| After a task's last answer | 13.9611 | 0.5641 | 103.2264 |
| Outside any task | 0.0671 | 0.0572 | 0.0739 |
| **Running time** | **5,418.2073** | **737.3566** | **3,255.4176** |

**The replay classed no request of A0's or A1's as held, and that is the reading's blind spot,
not the scheduler's behaviour.** The definition's warning came true: the client also lowers its
buckets to Groq's own remaining counts (`ModelBuckets.observe`), the ledger does not record those
headers, and a request held by such a correction reads as not held. How strongly is visible in a
diagnostic added once the figures were seen (`opened_during_gap`, labelled as such in the file):
for every request whose pool the replay says **reopened during the gap before it**, how long after
that reopening the request actually went out.

| Went out after its pool reopened in the replay | A1 | A0 | A2-cheap |
|---|---|---|---|
| Requests | 752 | 128 | 409 |
| Least / 5th percentile | 1.1725 / 1.1746 s | 2.3993 / 2.4005 s | 0.0023 / 0.0042 s |
| Median / 95th percentile | 2.6313 / 2.7762 s | 2.4017 / 2.4028 s | 1.4861 / 3.8987 s |
| Most | 3.7807 s | 2.4037 s | 4.3542 s |

On A0 it is **a constant 2.4 seconds** on all 128 — 320 tokens of per-minute refill. The live
client ran a fixed amount behind its declared bucket for the whole run, and the only code in the
scheduler that holds a pool its declared buckets have opened is `observe`. So on A0 and A1 **the
"not held" time is overwhelmingly waiting**, not work: split by what the replay saw, A1's 818
not-held gaps are 752 whose pool reopened during the gap — 4,548.9875 s, median 5.234 s, 90th
percentile 9.1847 s, 99th 16.0996 s, most 24.2783 s — and 66 whose pool was open when the work
ended, 8.7635 s in all with a median of 4.3 ms, which is what the agent's own work between turns
looks like. A0's are 128 (570.2553 s, median 3.9074 s) and 20 (8.3034 s, median 2.4 ms). On
A2-cheap the replay did see waiting — 115 requests held, 341.0034 s by the per-minute token
bucket and 15.4351 s behind a minute-scope block — and its not-held gaps are 294 whose pool
reopened (1,756.2299 s) and 373 whose pool was open (318.6289 s, median 6.6 ms, 90th percentile
2.8878 s).

**The queue wait as defined** — the gap before a held request, or one across a refusal, less
recorded local work; zero otherwise — is therefore reported, and says less than it was meant
to:

| Derived queue wait | A1 | A0 | A2-cheap |
|---|---|---|---|
| Requests / waiting zero | 827 / 818 | 150 / 148 | 837 / 667 |
| 50th / 90th / 99th percentile | 0.0 / 0.0 / 0.306 s | 0.0 / 0.0 / 4.9708 s | 0.0 / 2.7687 / 6.4708 s |
| Most / total | 6.6552 / 32.6657 s | 5.6808 / 10.6516 s | 9.0751 / 494.5158 s |

The not-held gaps whose pool reopened during the gap are, as the table above argues, where the
waiting went, and a reader who wants the distribution of time a request spent between the end of
its turn's work and going out should read those: **on A1 a median of 5.234 s and a 99th
percentile of 16.0996 s.** That reading was chosen after the figures were seen and is labelled
wherever it appears; the figure fixed in advance is the table just above. The tolerance barely
matters: at 0.01 s and at 0.2 s the held count is 0 and 0 on A1 and A0, and 74 and 114 on
A2-cheap against 115.

#### Where the loss went

| Seconds | A1 | A0 | A2-cheap |
|---|---|---|---|
| **Loss** — running time less the ceiling | **174.0799** | **2.9408** | **171.2621** |
| A bucket sat full during provider latency | 7.9262 | 0.5307 | 29.5389 |
| A bucket sat full at any other moment | 0.3677 | 0.0043 | 0.7589 |
| Capacity unused when the last request went out | 19.1268 | 2.4018 | 24.3147 |
| After the last answer | 6.0235 | 0.004 | 4.94 |
| **Difference** — what those do not account for | **140.6357** | **0.0** | **111.7096** |

- **Bucket starvation is the ceiling, not a loss.** Waiting for a declared bucket to refill is
  what the limits cost; it is inside the 5,244.1274 s.
- **Concurrency cap and provider latency cost 7.9262 s on A1, 0.5307 s on A0 and 29.5389 s on
  A2-cheap** — the time a bucket sat full with one request in flight and nothing else allowed
  out. A2-cheap's is the largest because three of its sessions were 35 to 50 s long, and every
  session starts with full buckets that a one-at-a-time scheduler spends one request at a time.
- **Groq refusing at minute scope cost almost nothing in throughput**: 0.3206 s of full bucket
  on A1 and 0.0476 s on A2-cheap across all 88 refusals, because a pool refused for the minute was
  a pool whose bucket was already in deficit and refilling anyway. The 32.6657 s and 138.0773 s
  spent across refusals is waiting that would mostly have happened regardless.
- **Capacity unused when the last request went out is the lag behind `observe`**: on A0 it is
  2.4018 s, the same constant 2.4 s as above, and on A1's first three sessions 3.7806, 3.3718 and
  2.3378 s.
- **The difference is zero, to within 0.6 s, in every session but four**, and those four are the
  sessions where Groq refused one of several pools for the day. There the ceiling, as fixed,
  credits the refused pool with a full per-minute bucket at the session's start and with the
  day's refill after the refusal, and the run used neither. From `refused_pools`, a diagnostic
  added once the figures were seen and labelled so in the file:

| Session | Pool refused for the day | Credited by the refusal / served | Credited after it / served | Difference |
|---|---|---|---|---|
| A1, 04:22:00Z | `groq#1` at 5.3819 s | 8,717.5865 / 4,187 | 3,866.1308 / 0 | 66.3536 s |
| A1, 04:50:10Z | `groq#1` at 3.8892 s | 8,518.5563 / 3,378 | 3,615.6936 / 0 | 74.8808 s |
| A2-cheap, 06:30:14Z | `groq#2` at 885.3991 s, and `groq#1` at 1,001.2553 s, the session's end | 126,053.2105 / 124,093 and 141,500.7105 / 141,155 | 1,237.5088 / 0 and none | 22.5312 s |
| A2-cheap, 10:50:28Z | `groq#1` at 200.6472 s, `groq#2` at 207.097 s | 34,752.9651 / 33,538 and 35,612.9375 / 34,140 | 3,190.4286 / 0 and 4,440.4985 / 0 | 89.1784 s |

The first column is capacity Groq would not have honoured: its day counter was already at the
limit when the session began, which the ceiling, starting every session full as the client
does, cannot know. The second is refill Groq *would* have honoured, at 200,000 tokens a day —
and **the client left it unused because it shuts a pool refused for the day for 3,600 s**
(`UNKNOWN_DAILY_REPROBE_S`, for a provider with no daily reset time), where Groq's own retry
hints on those refusals said 57, 144, 132, 236, 165 and 10 s (`retry_after_s`,
`runs/quota-walls.jsonl`). That is a real, small loss of this scheduler's: a Groq pool refused
for the day refills at about 2.3 tokens a second, one A1 request every seven minutes or so, and
the scheduler does not look again for an hour.

#### What changed between the reading and the figures

The reading was committed first (`d027fc7`); three things changed before the figures were
committed (`7a32ce7`), and none of them moved a ceiling, a running time or a ratio.

- **A defect, fixed to match the text above.** The first version of the code called a request
  held whenever its pool reopened anywhere in the gap before it. The definition says the pool must
  reopen within the tolerance of the send. Fixing it moved every A0 and A1 request from held to
  not held, which is how the `observe` lag was found.
- **How "capacity unused when the last request went out" becomes seconds.** It was first divided
  by the ceiling's rate at that instant. That misstated it in two A1 sessions — 03:01:17Z, which
  ran past the point where the day line overtakes the minute line, and 03:54:26Z, where nothing
  needed refill — so it is now counted back along the ceiling's own admission. **This was chosen
  after those two sessions' figures were seen.** It moves only the split between "unused" and
  "difference".
- **Two diagnostics were added after the figures were seen**, `opened_during_gap` and
  `refused_pools`. Both are labelled so in the file, and neither is part of the reading.

#### What this measurement is not

It is the scheduler against the limits as declared, on the demand these runs actually made. It
is **not** a statement about Groq's tiers, which are what they are, nor a throughput any
deployment would see: every run here was one agent, one request at a time, forced to
concurrency 1 by the per-minute token budget, on free tiers. And it rests on a replay that cannot
see the provider's remaining counts, which is why the time the scheduler spent waiting on them is
labelled rather than measured.

## 6.3 — The boundary, stated

**No requests-per-second figure exists for this project's service against live providers, and
none will be produced.** Such a number would describe Groq's rate limiter, not this system:
against 30 requests and 8,000 tokens a minute per pool, a service answering questions with A1's
loop serves what the per-minute token bucket admits, one trajectory at a time (concurrency 1 is
forced, constraints 46, 64 and 91), and any figure taken there would be the quota divided by
the tokens a question happens to cost. 6.1 above measures exactly that relationship, which is
the honest version of it.

**And no figure exists for this project's own code path either.** The roadmap planned a second
number here — requests per second and 95th-percentile latency of the service with the model
replaced by a stub (6.2) — and **6.2 was cut** by the author's decision on 2026-09-10, so the
roadmap's phrase "the two numbers above are what can honestly be claimed" does not hold. **One
number can be claimed: the scheduler's ratio to the ceiling the declared quotas allow**, 96.7871%
on A1's run (above, with its two companions). Nothing about how many requests a second this
project's service can take has been measured, and nothing in this repository should be read as
saying so.

The page 7.3 will deploy calls no model at all: under 7.3's narrowing it replays committed
trajectories, so the only throughput it will have is its static host's.
