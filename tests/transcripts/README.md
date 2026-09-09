# Transcript fixtures

Test material, not documentation — the same footing as
[`tests/provider_bodies/`](../provider_bodies/README.md), and this file says the same thing
that one does: **which of these are verbatim and which are not.**

3.4's acceptance asks for metrics "computed from committed transcripts", and a run directory
is gitignored (`/runs/`). So the transcripts that prove the metrics live here instead, as two
miniature run directories. Each holds a `ledger.jsonl` and a `transcripts/` beside it,
because **whether a task solved is not in a transcript and must not be** — constraint 51 —
and `metrics.compute` joins the two on `task_id`.

## `lifted/` — verbatim

**Every file is byte-for-byte what run `20260908-155422-c28177` wrote on 2026-09-08.** Groq,
`openai/gpt-oss-120b`, five tasks of `splits/smoke.json`, 28 requests. Nothing is edited,
trimmed or reordered; the ledger is the whole ledger and all five transcripts are present.

It is here for the one thing a hand-built file cannot do: prove the reader parses what the
writer actually emits, against output nobody designing the reader chose. **No number computed
from it is a result** — it is an acceptance run on the smoke set, and 3.6 is A1's measured
run.

| Task | What it happens to contain |
|---|---|
| `dev-0003` | `answer` in 3 turns, 2 calls, solved. `list_tables` first, then one `describe_table`. |
| `dev-0030` | `answer` in 4 turns, 3 calls, solved. |
| `dev-0126` | `answer` in 4 turns, 3 calls, **not** solved — `value_mismatch` on a `CAST` the reference does not do. |
| `dev-0207` | `tool_call_limit` at 12 calls, 6 of them `execute_sql`, no final SQL. The only trajectory here that ran out of room. |
| `dev-0317` | **A repair, live.** The model ran `SELECT COUNT(*) FROM Templates`, was shown `20`, and replied `20` — the count rather than the query. 3.3 rejected it, fed the error back, and the second reply was the statement. Solved. |

Two facts about this run that shaped the metrics rather than being described by them: **no
`execute_sql` in any of the five trajectories errored**, so the recovery rate's headline
denominator was 0 and `TBD` is what the reader gets; and `dev-0207` had two `execute_sql`
calls return no rows while its *first* returned one, which is why `metrics.py` reports
`trajectories_with_any_execute_sql_error` and `..._empty` as counts beside the three rates.

## `built/` — hand-built, not lifted

**None of these was produced by a run.** They are written to the format `transcript.py`
defines, and they exist because the metric edge cases have to be exact and a real run may
never contain them — the smoke run above contains four of the ten.

Each file is one case. The expected value of every metric over this directory is pinned in
`tests/test_metrics.py`; that is the real specification and this table is the map.

| Task | The case |
|---|---|
| `t-recovers` | First `execute_sql` errors, the model fixes it, the task solves. The **headline** recovery denominator, recovered. |
| `t-no-recovery` | First `execute_sql` errors and the task never solves. Same denominator, not recovered. |
| `t-empty-first` | First `execute_sql` succeeds with **no rows**, and the task solves. The `empty` denominator, which is reported apart because an empty result can be the correct answer. |
| `t-no-execute` | No `execute_sql` at all. In **none** of the three denominators. |
| `t-wasteful` | Three calls after the last contributing one — and two of them describe and sample a table the final query never names, which is exactly where the definition is approximate. |
| `t-turn-limit` | Ends at the turn limit with an assistant message carrying **two calls that were never executed**. Six asked for, four `tool_result` events, and the metric must count four. |
| `t-repaired` | A reply with no SQL, repaired into a solve — `dev-0317`'s shape, built so the assertion can be exact. |
| `t-failed` | The run's budget stopped it. The ledger says `failed` and `solved` is **absent, not false**: the model never got to be wrong, so the task is in no rate's denominator. |
| `t-retried` | Two `start`-to-`end` brackets in one file, as a resumed run appends. **The last stands.** Its second bracket continues `seq` from the first's highest rather than restarting at 1 — which is what `TranscriptWriter` does, and building it the wrong way first is how the fixture was found to be wrong rather than the reader. |
| `t-truncated` | No `end` event: a process killed before it could record its own death. It reads as incomplete, which is what a reader must be able to see. |

## Regenerating

Don't. These are the fixture, the way a recorded provider body is. `lifted/` cannot be
regenerated without spending quota on a different run, and a `built/` file rewritten to make
a failing test pass is a test that has stopped testing anything.
