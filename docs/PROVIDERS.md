# Providers

Free tiers only. No paid API spend anywhere in this project.

Everything in the observed columns below came back from a real call made by
[`scripts/provider_check.py`](../scripts/provider_check.py) on **2026-09-06**, recorded in
[`provider-check.json`](provider-check.json). That script is never run from a test: the
suite makes no live API calls and passes with no keys present.

## Verdict on the stop-gate

**Two providers usable. The gate required two, so it passes — at exactly the minimum.**

| Provider | Usable | Evidence |
|---|---|---|
| Groq | **yes** | 3 models answered, all called a tool |
| Google AI Studio | **yes** | 2 models answered, both called a tool |
| Cerebras | **no** | every call returns `402 payment_required`, "Payment required to access this resource. Visit your billing tab." |

Cerebras lists three models to this key and refuses to serve any of them without billing,
so there is no free tier here to build on. **The roadmap's second cut — "the third
provider" — is therefore already made, by circumstance rather than by choice.** Phases 1
and 6 still stand: two providers is enough to spread work, to spill over when a bucket
empties, and to compare cost. It leaves no slack if one of the two changes its terms.

## Verified models

Latency is one warm call, not a distribution. Tool calling was probed separately with a
one-function schema, because Phase 3's agent is tools all the way down and a model that
cannot call one is not a candidate however generous its quota. **Four tools across several
turns is a different exercise and was measured separately in 3.1** — see *Four tools, and
one call a turn* below.

| Provider | Model string | Chat | Tool call | Latency | Prompt/completion tokens |
|---|---|---|---|---|---|
| groq | `openai/gpt-oss-20b` | 200 | yes | 0.518 s | 78 / 24 |
| groq | `openai/gpt-oss-120b` | 200 | yes | 0.721 s | 78 / 49 |
| groq | `qwen/qwen3.8-27b` | 200 | yes | 0.407 s | 19 / 2 |
| google-ai-studio | `gemini-3.5-flash-lite` | 200 | yes | 0.830 s | 8 / 1 |
| google-ai-studio | `gemini-3.8-flash` | 429 † | yes † | — | — |
| cerebras | `qwen-3.8-27b`, `gpt-oss-120b`, `gemma-4-31b` | 402 | — | — | — |

† `gemini-3.8-flash` **does** answer and **does** call a tool — it did both on the first
probe of the day, at 1.136 s. It shows 429 in the current artifact because its ceiling is
20 requests per day and this session's own probing spent them. The model works; the quota
does not stretch. See the pair section below.

## Limits: documented beside observed

| Provider | Model | RPM documented | RPM observed | RPD documented | RPD observed | TPM documented | TPM observed | TPD documented | TPD observed |
|---|---|---|---|---|---|---|---|---|---|
| groq | `openai/gpt-oss-20b` | 30 | **30** | 1,000 | **1,000** | 8,000 | **8,000** | 200,000 | TBD |
| groq | `openai/gpt-oss-120b` | 30 | **30** | 1,000 | **1,000** | 8,000 | **8,000** | 200,000 | **200,000** |
| groq | `qwen/qwen3.8-27b` | 30 | not probed | 1,000 | **1,000** | 8,000 | **8,000** | 200,000 | TBD |
| google-ai-studio | `gemini-3.5-flash-lite` | not published | **15** | not published | TBD | not published | TBD | not published | TBD |
| google-ai-studio | `gemini-3.8-flash` | not published | **5** | not published | **20** | not published | TBD | not published | TBD |

**Documented limits.** Groq publishes a Free Plan table at
`https://console.groq.com/docs/rate-limits` (read 2026-09-06), columns MODEL ID · RPM ·
RPD · TPM · TPD. Google's rate-limit page at
`https://ai.google.dev/gemini-api/docs/rate-limits` (read 2026-09-06, page's own last-
updated 2026-09-02) explains the three dimensions but **no longer publishes per-model
free-tier figures** — it directs you to a signed-in AI Studio page. So for Google there is
no documented column to fill, and saying "not published" is the honest entry.

**Observed limits.** Groq's RPD and TPM were read from response headers, whose meaning its
documentation fixes: `x-ratelimit-limit-requests` always means RPD and
`x-ratelimit-limit-tokens` always means TPM. The RPD reading is corroborated by the reset
countdown, which advanced 86.4 s per request — exactly one thousandth of a day, which is a
1,000-per-day bucket refilling. RPM came from a burst of 40 concurrent requests: Groq's own
429 body names it, `on requests per minute (RPM): Limit 30`, with `retry-after: 2`. Google
serves no rate-limit headers, but its 429 body carries the quota it enforced —
`GenerateRequestsPerMinutePerProjectPerModel-FreeTier`, value **15** for
`gemini-3.5-flash-lite` and **5** for `gemini-3.8-flash`, with a `retryDelay` of 26 s.

**Two grades of evidence sit in those observed columns, and they are not the same.** A
figure is either one this project **was refused at** or one the provider **stated**. Groq's
RPM is the first kind: 40 concurrent requests, 11 refused, and a 429 body naming the
ceiling. Its RPD and TPM are the second: read out of headers on requests that succeeded,
with nothing here having reached either. Google's 15 and 5 are the first kind. Where a
number below is used to plan capacity, which kind it is matters, so the distinction is kept
rather than flattened into one word.

**Groq's daily allowance is per model, and the evidence for it is a pair of counters.**
After identical 40-request bursts, `x-ratelimit-remaining-requests` read **895** on
`openai/gpt-oss-20b` and **926** on `openai/gpt-oss-120b`. Two counters, not one. That is
what the run-budget line below rests on when it multiplies 1,000 requests by three models,
and it is also why Phase 1 keys its quota buckets on the model rather than on the provider.

**Where documented and observed can be compared, they agree.** Every Groq figure matched.
That is worth recording precisely because the item exists on the assumption that they
might not.

**Why the TPD column says TBD.** Observing a tokens-per-day ceiling means exhausting a
day's tokens, which costs the day. These fill in opportunistically from Phase 2's run
ledgers, where a full working-set run consumes real quota and the ledger records what
happened when it ran out. Deliberate, not an oversight.

**Phase 2's full run did not reach the ceiling.** It established a floor instead:
**106,740 tokens across 150 requests in one 12.3-minute session, with zero 429s and zero
quota walls**, on 2026-09-08, run `20260908-133316-faecd5`.

**Phase 3.6's run found it, and the TPD column is no longer TBD.** A1's measured run spent
**771,028 tokens across 827 requests** on 2026-09-10, run `20260910-024454-1f69bc`, ledger
`runs/20260910-024454-1f69bc/ledger.jsonl` — and Groq refused, naming the quota itself:

> `on tokens per day (TPD): Limit 200000, Used 199125, Requested 1314`

Recorded at `2026-09-10T03:54:20Z` in `runs/quota-walls.jsonl`, `scope: day`, which shut the
pool. **The published 200,000 is the enforced figure**, and the observed column now says so
on the strength of the provider's own 429 body rather than of a document.

**What the run did NOT establish is the window that counter runs over, and that is written
`TBD` rather than guessed.** By the time Groq said `Used 199125`, the run had spent 399,757
tokens in total — so its counter is plainly not cumulative from the start of a run, and the
retry hints on those refusals were 3m9s and 46s rather than hours, which is the shape of a
bucket that refills continuously rather than resetting on a boundary. This project's own
`TokenBucket` already models Groq that way, on the requests-per-day evidence recorded above.
Determining the exact window would cost another day of capacity and buys nothing yet.

**One caution for anyone reading a resumed run's spend.** The client's quota buckets are
**per process**: a new session starts them full, so the modelled daily ceiling bounds a
session rather than a day, and a chained run can spend past it before the provider's own
counter objects. That is safe because the bucket is only a guess and the 429 is the
authority — which is exactly what happened here, twice — but it is why 3.6's six sessions
spent 771,028 tokens against a modelled 200,000 a day.

## Credential pools

Google's quota is enforced **per Cloud project, not per key**, so a second key issued
inside the same project shares one ceiling while a key from a second project doubles the
headroom. Nothing in the key itself says which one you have.

Two pools are configured — `GEMINI_API_KEY` and `GEMINI_API_KEY_2` — and their
independence was established by behaviour rather than by reading the console. The probe
saturates the first pool, **confirms it is still refusing**, and calls the second one
inside that window:

| Step | Result |
|---|---|
| 20 concurrent requests on `GEMINI_API_KEY` | 20 × 429 — pool saturated |
| 3 control requests on `GEMINI_API_KEY`, immediately after | 3 × 429 — still refusing |
| 8 requests on `GEMINI_API_KEY_2`, in that window | **8 × 200** |

**Independent.** The control step is what makes this mean anything: without proof that the
first pool was refusing at that moment, the second pool answering would say nothing.

The convention, which Phase 1's registry implements: `<PROVIDER>_API_KEY` plus optional
`_2`, `_3`, … and **each suffixed credential is its own quota pool with its own token
bucket**, never a fallback credential for the same pool. Two Google projects means two
buckets of 15 RPM, not one bucket of 30.

**Groq has two pools as of 2026-09-10**, `GROQ_API_KEY` and `GROQ_API_KEY_2`, the second from
a separate Groq account. Its independence was established by the same probe shape and with
the same control step, because pool two answering means nothing without proof that pool one
was refusing at that moment:

| Step | Result |
|---|---|
| 40 concurrent requests on `GROQ_API_KEY` | **30 × 200, 10 × 429** — saturated at the observed 30 RPM |
| 1 control request on `GROQ_API_KEY`, immediately after | **429 — still refusing**, org `org_01knx5npqkfc19q5jefzfvdfv5` |
| 1 request on `GROQ_API_KEY_2`, inside that window | **200** |

**Independent.** And Groq's own refusals name the **organization** as the unit they enforce
against — every 429 body above and every quota wall in `runs/quota-walls.jsonl` carries it —
so a pool answering while another organization is refusing is a different organization, and
therefore a different daily token counter as well as a different per-minute one. 3.6's run
used both: **439 attempts on `groq#1`, 388 on `groq#2`**, one pinned model string throughout,
and every ledger attempt row records which pool served it.

## The pair

| Role | Provider | Model string |
|---|---|---|
| **cheap** | groq | `openai/gpt-oss-20b` |
| **strong** | groq | `openai/gpt-oss-120b` |
| spillover, cheap | google-ai-studio | `gemini-3.5-flash-lite` (× 2 pools) |
| ~~spillover, strong~~ | ~~google-ai-studio~~ | ~~`gemini-3.8-flash`~~ — **20 requests per day**, see below |

Both tiers on Groq, with Google as the second provider the client spreads onto. Reasons:

- **The capability step is clean.** 20b against 120b is the same family, same tokenizer,
  same tool-call format, differing in size. A cascade across that step measures escalation
  rather than measuring two vendors' prompt handling.
- **Groq's RPM is 30 against Google's 5 for the strong tier.** A 150-task agent run makes
  on the order of a thousand calls. At 5 RPM that is hours of wall clock before any token
  limit binds; at 30 RPM it is tens of minutes.
- **Google still earns its place**, as the provider work spills onto when a Groq bucket
  empties, and as the cross-provider comparison Phase 1.1 requires — but only on
  `gemini-3.5-flash-lite`.

**`gemini-3.8-flash` is not a spillover tier.** Its own 429 names the ceiling:
`GenerateRequestsPerDayPerProjectPerModel-FreeTier` = **20**. Twenty requests per day per
project — 40 across both pools. That is enough to verify the model answers and calls a
tool, which is what 0.4 needed it for, and nowhere near enough to serve any part of a
150-task run. It stays registered as a named model so the cascade can be pointed at it if
the strong tier ever needs to move, and it is excluded from every capacity figure below.

**The consequence, stated plainly: the strong tier has no fallback with real capacity.**
Both cheap and strong sit on Groq, and Google can only mirror the cheap one. If Groq's
`openai/gpt-oss-120b` becomes unavailable mid-project, there is no free strong model left
with a workable daily allowance, and the escalation half of Phase 5 stops being
measurable. This is the largest single-point-of-failure in the project's infrastructure
and it is a consequence of the free-tier constraint, not of a design choice.

**What would flip it.** If 2.4 and 3.6 show 20b and 120b too close to separate, the cascade
has no gap to exploit and the strong tier moves to `gemini-3.8-flash`, accepting 5 RPM. If
Groq's 200,000 tokens per day per model proves the binding constraint in practice, the bulk
of the work moves to Google and Groq becomes the spillover.

## The run budget

**Daily capacity, with each figure's evidence named.** Groq: three usable models × 1,000
requests and 200,000 tokens each — **3,000 requests and 600,000 tokens per day**, per
account. The 1,000 is header-stated and the per-model split is evidenced by the two counters
above; **the 200,000 was Groq's published figure and is now observed** — 3.6's run was
refused with `TPD: Limit 200000` on 2026-09-10, recorded above.

**Two cautions on that 600,000, both of which bit in 3.6.** It is three models *added
together*, so **a run that uses one role uses one model and has 200,000, not 600,000** — A1
uses `strong` only. And it is **per account**: since 2026-09-10 this project has two
independent Groq pools, so the figure doubles across pools while each pool's own ceiling
stays where it is.

Google: two independent project pools. Pool one serves `gemini-3.5-flash-lite` at 15 RPM,
observed. **"30 RPM combined" is an inference, not a measurement** — pool two was never
asked for more than 8 requests at once, so its own ceiling has never been reached and 15 is
assumed from it being the same free tier on the same model. Reasonable, and still an
assumption. Its RPD and TPD are TBD and remain the project's most valuable unmeasured
numbers — the ceiling is known to be above the burst probes in `provider-check.json`, since
flash-lite was still answering after them. `gemini-3.8-flash` contributes nothing: 20
requests per day per pool.

**Runs planned, counted honestly.** The A1-strong arm of Phase 5 reuses the 3.6 run rather
than repeating it, so it is not counted twice.

| Run | Phase | Tasks |
|---|---|---|
| A0, working set | 2.4 | 150 |
| A1, working set | 3.6 | 150 |
| A2-cheap, working set | 5.2 | 150 |
| A2 cascade, working set | 5.2 | 150 |
| Scheduler efficiency | 6.1 | 150 |
| Reserve | after 7 | 150 |
| Allowance for re-runs after defects | — | 450 (3 runs) |
| **Total** | | **1,350 task-runs** |

**What that costs is TBD**, because tokens per task is not measured until 2.4. The table
below is arithmetic over hypothetical per-task costs, not a prediction about this system,
and it is here to show where the cliff is:

| If a task costs… | One 150-task run | Groq-days for one run | Groq-days for all 1,350 |
|---|---|---|---|
| 2,000 tokens | 300,000 | 0.5 | 4.5 |
| 5,000 tokens | 750,000 | 1.25 | 11.3 |
| 10,000 tokens | 1,500,000 | 2.5 | 22.5 |
| 20,000 tokens | 3,000,000 | 5.0 | 45.0 |

**The finding: token budget binds long before wall clock does, and it may not fit the
calendar.** Three calendar weeks against a plan that needs 22 Groq-days at 10,000 tokens a
task does not close on Groq alone. Four things follow, and none of them is "measure fewer
tasks":

1. **Google's unknown daily capacity is now the most valuable unknown in the project.** It
   is measured for free by Phase 2's first full run and should be recorded the moment it is.
   Phase 1.3 made that automatic: a refusal naming a quota is written into the run's own
   ledger as it happens, so the run that walks into the wall is the run that measures it.
2. **The smoke set is load-bearing.** Fifteen tasks for iteration instead of 150 is the
   difference between an afternoon of debugging costing 2% of a Groq-day and 20% of one.
3. **The budget guard (1.4) and the resumable ledger (1.3) stop being nice-to-haves.** A
   run that must span a quota reset is the normal case here, not the failure case. Both
   shipped: a run resumes by run ID skipping tasks already answered, and carries its token
   ceiling across every resume while its wall clock starts fresh each session.
4. **If it still does not fit, runs get cut, not tasks** — in the roadmap's stated order,
   which takes the cascade's two runs out before it touches anything measured.

## Four tools, and one call a turn

**Measured 2026-09-08 in 3.1, nine requests, `docs/a1-tool-probe.json`.** Groq,
`openai/gpt-oss-120b`, three tasks from `splits/smoke.json`, three turns each, all four of
A1's tool schemas offered on every turn. It is here rather than in a phase write-up because
these are facts about how this provider's model behaves, and a later session sizing a turn
limit or a prompt budget will come looking for them beside the quota numbers.

The table above proves a model calls **a** tool. None of the following follows from it.

| | |
|---|---|
| Called `list_tables` first, before naming any table | **3 of 3 tasks** |
| Tools called at least once, of four offered | **4 of 4** — none went unused |
| Maximum tool calls in one assistant message | **1** |
| Turns observed emitting more than one call | **0 of 9** |
| Prompt growth per turn | **+50 to +107 tokens** (466 → 516 → 601) |

**The finding that matters for a turn limit: this model does not batch.** The OpenAI
tool-call format permits several calls in one message and this model used one, in every turn
of every task. **A turn is therefore a tool call**, so a turn limit has to absorb one request
per table the model chooses to describe — and a session that read the format and assumed
batching would derive a limit less than half the size it needs. `src/query_pilot/agents/a1.py`
derives `TURN_LIMIT` against this.

**It also describes selectively rather than exhaustively** — one table per trajectory, not
all four or all six of the schema — which is why prompt growth is far below what a
describe-everything trajectory would cost, and why A1's projected token cost for a full
working-set run is a fraction of a first estimate. `docs/a1-tool-sizes.json` holds the
worst-case arithmetic these numbers are the realistic counterpart to.

**What this run does not record:** the served model string. The probe script did not carry
`model_returned` at the time, and it is written up as *not recorded* rather than inferred.
The requested model is read from `config/providers.toml` at the `strong` role, whose
spillover list is empty, so no other endpoint could have served these calls. The script
records both per turn from now on. **No accuracy figure was taken from this run** and none
may be — it ran on the smoke set and reports tool and turn behaviour only.

## The whole loop, once, against the same model

**Measured 2026-09-08 in 3.3, twenty-eight requests, run `20260908-155422-c28177`,
`runs/20260908-155422-c28177/ledger.jsonl`.** Groq, `openai/gpt-oss-120b`, five tasks from
`splits/smoke.json`, all four tools offered, a hard request ceiling of 80. The probe above
drove three turns of tool calling; this drove **whole trajectories** — discovery, querying,
answering, and 3.3's validation and repair. The transcripts and ledger are committed verbatim
under `tests/transcripts/lifted/`.

**No accuracy figure was taken from this run and none may be reported.** It ran on the smoke
set and reports loop and provider behaviour only. 3.6 is A1's measured run.

| | |
|---|---|
| Requests, against a ceiling of 80 | **28** |
| Tasks complete / failed | **5 / 0** |
| Tokens, prompt + completion | **23,880 + 2,541 = 26,421** |
| Tokens per task | **5,284.2** |
| Wall clock | **138.9 s** |
| Terminations | **4 `answer`, 1 `tool_call_limit`** |
| Turns emitting more than one tool call | **0 of 23** |
| Tool calls by name | `execute_sql` 9, `list_tables` 5, `describe_table` 5, `sample_rows` 3 |
| `execute_sql` calls that errored | **0 of 9** |
| `execute_sql` calls returning no rows | **2 of 9** |
| Repairs attempted / succeeded | **1 / 1** |

**One call a turn held again, and the two runs together make it 32 of 32.** Nine turns in the
3.1 probe and twenty-three here, and not one emitted a second call. `TURN_LIMIT` is derived
against this and a session assuming batching would size it at less than half what it needs.

**Twenty-three turns made a tool call and twenty-two calls ran.** The missing one is
`dev-0207`'s last: it hit `TOOL_CALL_LIMIT` and the call in that assistant message was never
executed, because there was no turn left to feed the result into. That is a real instance of
the thing 3.4's counting rule exists for — **tool calls per task counts `tool_result` events,
never the calls a message asked for** — and it appeared in the first five tasks.

**Not one `execute_sql` errored.** The model's SQL ran every time, which is a fact about this
model on this substrate and not a general one — and it is the risk 3.6 inherits, because
recovery rate's headline denominator is *tasks whose first `execute_sql` errored*. Over these
five it was **0**, and the metrics file correctly reads `TBD` rather than 0.0. Two calls did
return no rows, both inside `dev-0207` and both after a first call that returned one, so
recovery behaviour a first-call rule cannot see was already visible at five tasks.
`agents/metrics.py` reports `trajectories_with_any_execute_sql_error` and `..._empty` as
counts beside the rates for exactly this reason.

**5,284 tokens a task against A0's 712 is 7.4x**, which projected a 150-task A1 run at
roughly **790,000 tokens**. That projection was arithmetic over five tasks and not a
measurement. **3.6 has now replaced it: 771,028 tokens over 150 tasks — 5,140.2 a task, 7.22x
A0's — run `20260910-024454-1f69bc`, 2026-09-10.** The five-task projection was within 2.5%
of the measured total, which is luck rather than method and is recorded because the next
projection of this kind should not be trusted any further for it. What it does settle is that the run is affordable at concurrency 1, which
constraint 46 requires anyway: A1's worst single attempt is 8,000 tokens, the strong
endpoint's entire per-minute budget.

**The repair fired once and is worth reading.** On `dev-0317` the model ran
`SELECT COUNT(*) FROM Templates`, was shown `20`, and replied **`20`** — the count instead of
the query. Validation rejected it, the error went back, and the second reply was the
statement. `tests/transcripts/lifted/dev-0317.jsonl`.

---

## Findings the calls turned up

Four things that a documentation read would not have produced.

**A model retired under us on day one.** `gemini-2.5-flash` is listed by the models
endpoint and returns 404 on use: *"This model models/gemini-2.5-flash is no longer
available to new users."* This is exactly the risk the roadmap names — a free provider
retiring or renaming a model mid-project — appearing before a line of client code exists.
It is the argument for 1.1's pinned model strings in configuration, and for 1.5's test of
the terminal `model not found` path, made concrete on the first day.

**Both Groq and Cerebras sit behind Cloudflare, which answers Python's default User-Agent
with `error code: 1010` and HTTP 403.** The first run of this script reported every Groq
model as a 403 auth-shaped failure while `curl` succeeded with the same key. It was a bot
block on the client fingerprint. Phase 1's client must send a real User-Agent, and 1.2's
error classification must not read a 403 as a terminal auth failure without looking at the
body — this one is retryable-after-fixing, and neither category covers it cleanly.

**`gemini-3.8-flash` returned a 503 mid-probe** and succeeded on the next run. Free tiers
return transient server errors, and 1.2's retryable class needs to hold 503 as well as 429.

**Groq rejects the model's own malformed tool call with HTTP 400, and it is the caller's
task that dies.** Observed once in 827 requests, on `dev-0758` during 3.6:

> `400 {"error":{"message":"Failed to parse tool call arguments as JSON",`
> `"type":"invalid_request_error","code":"tool_use_failed",`
> `"failed_generation":"{\"name\": \"execute_sql\", \"arguments\": {\"sql\":\"SEL…`

The model emitted a tool call whose `arguments` were not valid JSON and **the provider, not
this project, refused it**. Two things follow and both are already true rather than needing a
change. It classifies **`bad_request`**, which is deliberately outside `FATAL_ERROR_CLASSES`
— that class can be about one task's content, so it fails the **task** and not the **run**,
and a resume retries it, which is what happened and it then succeeded. And because the task is
recorded `failed` rather than answered-wrongly, **it cannot move an accuracy figure**. Any
future session tempted to make `bad_request` fatal would be ending a measured run over one bad
generation.

An agent with tools has this failure mode and a single-shot agent does not, so it is a cost of
the loop rather than of the provider. Phase 4 drives the model harder than 3.6 did and should
expect to see more of it.

**A daily refusal asks to be retried in 32 seconds.** Found on 2026-09-07 by re-reading the
body already recorded here, while building the client — no further quota spent. The 429 on
`gemini-3.8-flash` carries a `RetryInfo` detail of `retryDelay: 32s` **beside** a `quotaId`
of `GenerateRequestsPerDayPerProjectPerModel-FreeTier`. The two contradict each other: the
hint says half a minute, the quota does not move until midnight Pacific. A client that
honours the hint retries every 32 seconds for the rest of the day and never succeeds.
**The quota named in the body outranks the retry hint the same body gives**, and Phase 1.2
disregards the hint on exactly this one case.

**Truncating an error body cost a fixture.** `provider_check.py` caps throttle messages at
300 characters, so although this document quotes
`GenerateRequestsPerMinutePerProjectPerModel-FreeTier` = 15, **no per-minute body survives
intact anywhere in the artifact** — the value was extracted at probe time and the document
it came from was cut. Phase 1's test fixture for that case had to be reconstructed from the
recorded daily body, and the client now keeps error bodies whole. A quota a provider names
once is worth more than the bytes it takes to store.

**A daily ceiling of 20 requests was found by accident, and only because a 429 body was
read rather than counted.** `gemini-3.8-flash` stopped answering partway through the day,
and the reason was not the 5 RPM already recorded — it was
`GenerateRequestsPerDayPerProjectPerModel-FreeTier` = 20, a different quota entirely. Two
lessons for Phase 1.2. A 429 is not one condition: the same status code covers a limit that
clears in seconds and a limit that clears at midnight Pacific, and a client that backs off
identically for both will sit retrying a request that cannot succeed today. **The error
classifier must read the quota named in the body, not just the status code**, and the
ledger must record it so the next run knows which wall it hit.

## What Phase 1 did with these findings

Recorded here so this document stays the place the provider facts live, and so a reader
does not have to open the client to learn which of them turned into behaviour.

| Finding | Where it landed |
|---|---|
| Two pools are two quota ceilings | Buckets keyed on provider, **pool and model** together |
| RPD is per model (the 895/926 counters) | The same keying; a provider-wide bucket would be wrong in both directions |
| Google publishes no per-model daily figure | That limit is **absent** from `config/providers.toml`, meaning unmodelled rather than unlimited — the client cannot pre-empt that wall, only record it |
| A 429 is not one condition | The classifier reads the quota named in the body; a per-minute wall moves the work, a per-day wall shuts the pool until the boundary |
| A daily refusal's 32 s retry hint | Deliberately disregarded when the quota it names is a daily one |
| Cloudflare's `error code: 1010` | Its own terminal class, `bot_block`, explicitly not `auth` |
| `gemini-2.5-flash` retired on day one | Model strings pinned in `config/providers.toml`; no call site can name one |
| Both providers call tools, and neither in the same shape | Normalised in `providers/`: Groq's `arguments` arrive as a **JSON string**, Google's `args` as an **object**. **1.5 found what that asymmetry had cost** — the Google side carried no guard, so a `functionCall` whose `args` was not an object escaped the client as a bare `ValueError`, which is not a `ClientError` and so was never classified. A run would have filed a provider's malformed answer under `executor_error`, the one label 1.3 reserves for this project's own bugs. Both sides now refuse it as `malformed_response` |
| A transient 503 | Retryable, with exponential backoff and jitter |
| Groq's RPD reset advancing 86.4 s per request | Modelled as continuous refill, with **no** daily reset boundary — unlike Google, which resets at midnight Pacific |
| Cloudflare answers Python's default agent | The client sends a real User-Agent on every request |
| Google's daily allowance is still unmeasured | 1.2's quota-wall sink now folds into the run ledger as a `wall` row (1.3), so the first run to reach that ceiling records it whole and **in order beside the attempts around it** — which is what makes the number reconstructable rather than merely stored |
| Groq's per-model daily counters, and two Google pools | Every ledger attempt row records `pool` and `model_returned` beside `model`, because a run spanning two projects must be able to say which served what, and Google answers a pinned request with its own build string |
| **600,000 tokens a day** of stated Groq capacity (3 models × a published 200,000 TPD) | What the committed run token ceiling traces to: `config/runs/working-set.toml` declares **1,500,000**, one 150-task run at the cliff table's 10,000-tokens-a-task row, which is 2.5 Groq-days. Both halves of that derivation are documented arithmetic rather than measurement, and the file says so |
| **711.6 tokens a task**, measured over 150 (2.4, run `20260908-133316-faecd5`) | What supersedes the 10,000-a-task **hypothetical** above as the basis for any *future* run declaration. The committed ceiling was deliberately left at 1,500,000 for 2.4 itself — the only evidence to lower it beforehand was ten smoke tasks, and 2.4 is the run that produced the real figure. The consequence is stated rather than hidden: the guard never bound, so that path is unit-tested rather than exercised live |
| The strong endpoint's **8,000 TPM**, against `wait_ceiling_s` of **60 s** | The condition every run declaration must satisfy: **`concurrency × tokens-per-attempt < tpm`**. Tokens are charged when answers arrive, so `concurrency` requests burst before any is paid for; charged together they put the per-minute token bucket into that deficit, and a deficit deeper than 60 s of refill makes the client raise `AllPoolsExhausted` — which the run treats as fatal and ends as `pools_exhausted` **with no provider having refused anything.** Measured at A0's sizes, concurrency 8 ended a simulated 150-task run at task 91, so `config/runs/working-set.toml` declares **1**. `docs/a0-prompt-sizes.json` holds the sizes and `tests/test_run_budget.py` holds the arithmetic. **This binds Phase 3 harder than Phase 2**: A1's loop costs multiples of 711.6 a task, so even concurrency 2 may be unsafe |
| Groq's **observed** 30 RPM | The floor the wall-clock ceiling is set above: 150 tasks at 30 RPM is 300 s of pure request time, and the committed ceiling of 14,400 s sits far above it because it exists to stop a run that is stuck, not one that is slow |
| A run must span a quota reset, and that is the normal case | The token ceiling is **inherited** across resumes and the wall-clock ceiling is **per session** (1.4). Tokens are a stock; time is a rate, and a clock counting the hours a run was not running would abort it for waiting |

## Reproducing this

```
set -a && . ./.env && set +a
uv run python scripts/provider_check.py
```

Keys are read from the environment (`GROQ_API_KEY`, `GEMINI_API_KEY`, `GEMINI_API_KEY_2`,
`CEREBRAS_API_KEY`), sent in headers, never placed in a URL, and never written to the
report. A provider with more than one pool configured also gets the independence probe.

**This script spends real quota** — roughly 120 requests, including two 40-request bursts
on Groq and the Google saturation probe, which alone exhausts `gemini-3.8-flash` for the
day. Do not run it casually.
