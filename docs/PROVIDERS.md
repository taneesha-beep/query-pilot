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
cannot call one is not a candidate however generous its quota.

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
| groq | `openai/gpt-oss-120b` | 30 | **30** | 1,000 | **1,000** | 8,000 | **8,000** | 200,000 | TBD |
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
requests and 200,000 tokens each — **3,000 requests and 600,000 tokens per day**. The 1,000
is header-stated and the per-model split is evidenced by the two counters above; **the
200,000 is Groq's published figure and has never been reached here**, so the token half of
that total is documented arithmetic rather than a measurement.

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
2. **The smoke set is load-bearing.** Fifteen tasks for iteration instead of 150 is the
   difference between an afternoon of debugging costing 2% of a Groq-day and 20% of one.
3. **The budget guard (1.4) and the resumable ledger (1.3) stop being nice-to-haves.** A
   run that must span a quota reset is the normal case here, not the failure case.
4. **If it still does not fit, runs get cut, not tasks** — in the roadmap's stated order,
   which takes the cascade's two runs out before it touches anything measured.

## Findings the calls turned up

Three things that a documentation read would not have produced.

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
| A transient 503 | Retryable, with exponential backoff and jitter |
| Groq's RPD reset advancing 86.4 s per request | Modelled as continuous refill, with **no** daily reset boundary — unlike Google, which resets at midnight Pacific |
| Cloudflare answers Python's default agent | The client sends a real User-Agent on every request |

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
