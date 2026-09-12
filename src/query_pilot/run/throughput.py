"""Scheduler efficiency, 6.1: how close the scheduler came to what the declared quotas allow.

**Read from ledgers already written; nothing here spends a request.** How it is read was fixed
in `docs/PERFORMANCE.md` before any ratio but A0's glimpse existed (`d027fc7`), and this module
is that text in code. The short version, because every function below serves one clause of it:

- **The ceiling** of a session is the least time in which any scheduler admitting requests the
  way the declared buckets do could serve that session's answered requests and their recorded
  tokens: buckets per pool, starting full (the client's own are per process, constraint 74),
  each pool's last request forgiven its tokens, capacity spread over the pools present in any
  proportion, and a day-scope refusal taken as the provider's statement of what that pool has
  left. The latest of the request and token floors, plus the last answered request's latency.
- **Running time**, never wall clock, is the base. Operator gaps are reported beside it.
- **Every second of a session** falls in one of seven categories, split along the ledger's own
  timestamps, and they sum to its running time by construction.
- **The queue wait is derived**, because no file records it: each session is replayed through
  the client's own :class:`~query_pilot.client.buckets.ModelBuckets` on a clock set to the
  ledger's timestamps, and a request whose pool opened during the gap before it was *held*.
- **The loss** — running time less the ceiling — is the time a pool's binding bucket sat full
  while the ceiling counted on its refill, charged to what the session was doing, plus the time
  after its last answer; what that does not explain is stated rather than spread.

**This package's seam holds** (constraints 16, 17): ``run`` imports the client's buckets and
nothing in the client learns this exists. Transcripts belong to ``agents``, which imports
``run``, so the recorded tool time a transcript carries comes in as a plain mapping from the
script rather than by importing the reader here.

**The inputs are rounded to the microsecond before anything is computed**, and the committed
file carries them, so `tests/test_throughput.py` can recompute every figure from the committed
file alone and require it back exactly — the ledgers are gitignored and CI never sees them.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Any, Final

from query_pilot.client.buckets import DAY_S, MINUTE_S, ModelBuckets, TokenBucket
from query_pilot.client.config import Limits

__all__ = [
    "CATEGORIES",
    "DEFINITIONS",
    "SENSITIVITY_S",
    "TOLERANCE_S",
    "Request",
    "Session",
    "TaskSpan",
    "Wall",
    "compute_run",
    "compute_session",
    "document",
    "dumps",
    "regenerate",
    "run_document",
    "session_from_document",
    "session_to_document",
    "sessions_from_ledger",
]

TOLERANCE_S: Final = 0.05
"""How close a pool's replayed opening must be to the send for the send to count as held.

About thirty times the largest `asyncio.sleep` overshoot measured on the machine that took every
run — 1.53 ms over 300 sleeps of 20 ms, median 1.12 ms, 2026-09-12 — to cover the ledger stamping
an attempt after its answer has been parsed and handed back up through the agent. Chosen, not
measured; the classifications that would change at each of :data:`SENSITIVITY_S` are reported.
"""

SENSITIVITY_S: Final = (0.01, 0.2)

LATENCY: Final = "provider_latency"
HELD: Final = "held_by_buckets"
ACROSS: Final = "across_a_refusal"
LOCAL: Final = "recorded_local_work"
NOT_HELD: Final = "not_held"
AFTER: Final = "after_last_answer"
OUTSIDE: Final = "outside_any_task"
CATEGORIES: Final = (LATENCY, HELD, ACROSS, LOCAL, NOT_HELD, AFTER, OUTSIDE)

# Which bucket held a request, named for what it is rather than for a field.
_HOLDERS: Final = (
    "tokens_per_minute",
    "requests_per_minute",
    "tokens_per_day",
    "requests_per_day",
    "blocked_minute",
    "blocked_day",
)

DEFINITIONS: Final[Mapping[str, str]] = {
    "ceiling": (
        "Per session: the least time in which any scheduler admitting requests the way the "
        "declared buckets do could serve the session's answered requests and their recorded "
        "tokens. Buckets per pool from config/providers.toml, refilling at limit / period and "
        "starting full at the session's start; a request goes out when no bucket on its pool is "
        "in deficit and its tokens are charged when it is answered, so each pool's last request "
        "in the session is forgiven its tokens; capacity is spread over the pools present in "
        "any proportion; from a day-scope refusal on, that pool's day bucket stands at the "
        "refusal's Limit - Used and refills from there. The latest of the request and token "
        "floors, plus the latency of the session's last answered request. A run's ceiling is "
        "the sum over its sessions."
    ),
    "pools_present": (
        "A pool that served an attempt in the session or was refused in it. The scheduler tries "
        "every present pool once the first is in deficit, so one that never appears was absent."
    ),
    "running_s": (
        "The sum over a run's sessions of ended_at - started_at, which is checked against "
        "run_end.elapsed_s. Operator gaps between sessions are reported beside it, never in it."
    ),
    "ratio": "The run's ceiling / its running time.",
    "tasks_per_minute": (
        "Achieved: tasks completed / running minutes. Ceiling: the same tasks / ceiling minutes."
    ),
    "steady_s": (
        "Beside the ratio, never its denominator: demand at the steady rate pools x limit per "
        "minute, with no full bucket at the start and nothing forgiven. A session can beat it."
    ),
    "concurrency_1_floor_s": (
        "Provider latency plus recorded local work: what no scheduler with one request in "
        "flight can overlap. Concurrency 1 is a run declaration, forced for A1 and A2-cheap "
        "(constraints 46, 64, 91), and is not in the ceiling."
    ),
    "time": (
        "Every second of running time once: provider_latency (latency_s of answered "
        "requests); held_by_buckets (the gap before a request, less recorded local work, when "
        "the replay says its pool opened during that gap); across_a_refusal (the same, when a "
        "429 fell inside the gap); recorded_local_work (tool elapsed_s from the transcript, "
        "which list_tables and describe_table do not record); not_held (the rest of a gap the "
        "replay says no bucket was holding: unrecorded work, or waiting the replay cannot see); "
        "after_last_answer (a task's time after its last answered request: validation, scoring, "
        "any request refused with a 400); outside_any_task (the run loop, session start and "
        "end). Timestamps that overlap are clamped, and the amount is reported as overlap_s."
    ),
    "queue_wait": (
        "Derived, never recorded. For a held request or one across a refusal, the gap before it "
        "less recorded local work: an upper bound, since unrecorded work before the wait began "
        "cannot be separated out. Zero for a request the replay says was not held."
    ),
    "replay": (
        "Each session replayed through the client's own ModelBuckets on a clock set to the "
        "ledger's timestamps: fresh buckets per pool at the session's start, a request charged "
        "when it went out, its tokens when it was answered, each 429 charged a request and its "
        "block applied when observed. It cannot see the client lowering its buckets to the "
        "provider's remaining counts (ModelBuckets.observe), which no ledger records."
    ),
    "held": (
        "The chosen pool's replayed opening, evaluated once recorded local work is done, lies "
        "more than the tolerance after that moment and within the tolerance of the send. A pool "
        "that opened earlier than that was open while the agent was still working: not held. "
        "One that opened later than the send plus the tolerance is replay_later_than_send, and "
        "its time is not_held."
    ),
    "loss": (
        "Running time less the ceiling, in four parts that sum to it exactly. full_bucket_s: "
        "before the session's last request went out, the time a present pool's binding bucket "
        "sat full while the ceiling counted on its refill, weighted by that pool's share of the "
        "ceiling's rate and charged to the category the session was in. unused_at_last_send_s: "
        "what the binding buckets still held when the last request went out, every other pool's "
        "last request given back its forgiven tokens, as the time the ceiling's own admission "
        "takes to accrue it, counted back from that send and never past the session's start. "
        "after_last_answer_s: the session's last answer to its end (all of it when nothing was "
        "answered). difference_s: what those do not account for — zero for one pool whose "
        "per-minute bucket binds throughout; otherwise stated, never spread."
    ),
    "opened_during_gap": (
        "A diagnostic, added once the figures were seen, and not a figure of the reading: for "
        "every request whose pool the replay says opened during the gap before it, how long "
        "after that opening the request actually went out; and the not_held gaps split by "
        "whether the pool was already open when recorded work ended or opened during the gap. "
        "Where the first is well above the tolerance, the live client was holding a pool its "
        "declared buckets had opened — which in this scheduler only ModelBuckets.observe does "
        "— and held_by_buckets reads low."
    ),
    "refused_pools": (
        "A diagnostic, added once the figures were seen: for each pool Groq refused for the day "
        "inside a session, the tokens the ceiling credited it before the refusal and after it, up "
        "to the session's last send, beside the tokens it served in each stretch."
    ),
    "repeated_work": (
        "Tasks whose trajectory was cut off at a session's end and retried in a later one: "
        "their requests, tokens and seconds. Served demand, so inside the ceiling; reported "
        "beside the ratio."
    ),
}


# -- inputs ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Request:
    """One answered request, in seconds from its session's start."""

    send_s: float
    latency_s: float
    tokens: int
    pool: str
    task: int
    local_after_s: float = 0.0

    @property
    def answer_s(self) -> float:
        return self.send_s + self.latency_s


@dataclass(frozen=True, slots=True)
class TaskSpan:
    start_s: float
    end_s: float
    status: str


@dataclass(frozen=True, slots=True)
class Wall:
    """One 429 inside the session. ``limit`` and ``used`` come from its body where it has them."""

    at_s: float
    pool: str
    scope: str
    blocked_for_s: float
    quota_id: str | None = None
    limit: int | None = None
    used: int | None = None


@dataclass(frozen=True, slots=True)
class Session:
    started_at: str
    running_s: float
    pools: tuple[str, ...]
    limits: Limits
    tasks: tuple[TaskSpan, ...]
    requests: tuple[Request, ...]
    walls: tuple[Wall, ...] = ()
    elapsed_s: float | None = None

    def __post_init__(self) -> None:
        if self.running_s < 0:
            raise ValueError(f"session {self.started_at}: negative running time")
        known = set(self.pools)
        for request in self.requests:
            if request.pool not in known:
                raise ValueError(f"session {self.started_at}: request on unknown {request.pool}")
            if not 0 <= request.task < len(self.tasks):
                raise ValueError(f"session {self.started_at}: request names task {request.task}")
            if request.latency_s < 0 or request.tokens < 0 or request.local_after_s < 0:
                raise ValueError(f"session {self.started_at}: a request has a negative figure")
        for wall in self.walls:
            if wall.pool not in known:
                raise ValueError(f"session {self.started_at}: wall on unknown {wall.pool}")
            if wall.scope not in ("minute", "day"):
                raise ValueError(f"session {self.started_at}: wall of scope {wall.scope!r}")
        sends = [request.send_s for request in self.requests]
        if sends != sorted(sends):
            raise ValueError(f"session {self.started_at}: requests are not in send order")
        starts = [task.start_s for task in self.tasks]
        if starts != sorted(starts):
            raise ValueError(f"session {self.started_at}: tasks are not in start order")


# -- the ceiling -------------------------------------------------------------------------------


def _line(limit: int | None, period_s: float) -> tuple[float, float]:
    """A bucket's cumulative admission from full: capacity and rate. Unmodelled is unbounded."""
    if limit is None:
        return math.inf, 0.0
    return float(limit), limit / period_s


@dataclass(frozen=True, slots=True)
class _Admission:
    """What one pool's buckets of one kind let through by time t, from the session's start.

    ``min`` of the per-minute and per-day lines from full, and from each day-scope refusal on, the
    refusal's stated remainder plus the day rate — the provider's own count, taken as it said.
    """

    minute: tuple[float, float]
    day: tuple[float, float]
    anchors: tuple[tuple[float, float, float], ...] = ()  # (at, value at that moment, left)

    def base(self, t: float) -> float:
        return min(self.minute[0] + self.minute[1] * t, self.day[0] + self.day[1] * t)

    def __call__(self, t: float) -> float:
        value = self.base(t)
        for at, value_at, left in self.anchors:
            if t >= at:
                value = min(value, value_at + left + self.day[1] * (t - at))
        return value

    def active(self, t: float) -> str:
        """Which line gives the minimum at t: ``minute``, ``day`` or ``anchor``."""
        minute = self.minute[0] + self.minute[1] * t
        day = self.day[0] + self.day[1] * t
        best, name = (minute, "minute") if minute <= day else (day, "day")
        for at, value_at, left in self.anchors:
            if t >= at and value_at + left + self.day[1] * (t - at) < best:
                return "anchor"
        return name

    def slope(self, t: float) -> float:
        name = self.active(t)
        return self.minute[1] if name == "minute" else self.day[1]

    def with_anchor(self, at: float, left: float) -> _Admission:
        return _Admission(self.minute, self.day, (*self.anchors, (at, self(at), left)))


def _admissions(session: Session) -> tuple[dict[str, _Admission], dict[str, _Admission]]:
    """Per pool: token admission and request admission, with day refusals taken in order."""
    limits = session.limits
    tokens = {
        pool: _Admission(_line(limits.tpm, MINUTE_S), _line(limits.tpd, DAY_S))
        for pool in session.pools
    }
    requests = {
        pool: _Admission(_line(limits.rpm, MINUTE_S), _line(limits.rpd, DAY_S))
        for pool in session.pools
    }
    for wall in sorted(session.walls, key=lambda w: w.at_s):
        if wall.scope != "day":
            continue
        left = 0.0
        if wall.limit is not None and wall.used is not None:
            left = float(max(0, wall.limit - wall.used))
        if wall.quota_id == "TPD":
            tokens[wall.pool] = tokens[wall.pool].with_anchor(wall.at_s, left)
        elif wall.quota_id == "RPD":
            requests[wall.pool] = requests[wall.pool].with_anchor(wall.at_s, left)
        else:
            raise ValueError(f"a day-scope refusal naming {wall.quota_id!r} is not modelled")
    return tokens, requests


def _total(admissions: Iterable[_Admission], t: float) -> float:
    return sum(admission(t) for admission in admissions)


def _least_time(admissions: Iterable[_Admission], demand: float) -> float:
    """The least t >= 0 at which the pools together admit ``demand``. Bisection, deterministic."""
    pools = list(admissions)

    def total(t: float) -> float:
        return sum(admission(t) for admission in pools)

    if demand <= 0 or total(0.0) >= demand:
        return 0.0
    hi = 1.0
    while total(hi) < demand:
        hi *= 2.0
        if hi > 1e10:
            raise ValueError(f"demand {demand} is beyond anything these buckets admit")
    lo = 0.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if total(mid) >= demand:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-9:
            break
    return hi


def _ceiling(session: Session) -> dict[str, Any]:
    requests = session.requests
    if not requests:
        return {
            "tokens_floor_s": 0.0,
            "requests_floor_s": 0.0,
            "binding": None,
            "last_latency_s": 0.0,
            "ceiling_s": 0.0,
        }
    token_admissions, request_admissions = _admissions(session)
    last_on_pool: dict[str, int] = {}
    for request in requests:
        last_on_pool[request.pool] = request.tokens
    token_demand = sum(r.tokens for r in requests) - sum(last_on_pool.values())
    tokens_floor = _least_time(token_admissions.values(), token_demand)
    requests_floor = _least_time(request_admissions.values(), len(requests))
    binding = "tokens" if tokens_floor >= requests_floor else "requests"
    last = requests[-1]
    return {
        "tokens_floor_s": tokens_floor,
        "requests_floor_s": requests_floor,
        "binding": binding,
        "last_latency_s": last.latency_s,
        "ceiling_s": max(tokens_floor, requests_floor) + last.latency_s,
    }


def _steady_s(session: Session) -> float | None:
    """The literal steady rate: no starting bucket, nothing forgiven. A session can beat it."""
    limits, pools = session.limits, len(session.pools)
    if not session.requests or pools == 0 or limits.tpm is None or limits.rpm is None:
        return None
    tokens = sum(r.tokens for r in session.requests)
    return max(
        tokens / (pools * limits.tpm / MINUTE_S),
        len(session.requests) / (pools * limits.rpm / MINUTE_S),
    )


# -- the replay --------------------------------------------------------------------------------


class _ReplayClock:
    """A clock the replay sets by hand to each ledger timestamp. Nothing ever sleeps on it."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def now_utc(self) -> datetime:
        raise NotImplementedError("the buckets never ask a replay clock for the date")

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError("nothing waits on a replay clock")


@dataclass(frozen=True, slots=True)
class _Probe:
    """What the replay saw for one request."""

    gap_start: float
    local: float
    remainder: float  # the gap after recorded local work
    opened_after: float  # chosen pool's replayed opening, less the end of local work
    holder: str | None
    another_pool_open: bool
    refusal_scope: str | None


def _holder(buckets: ModelBuckets, blocked_scope: str | None) -> tuple[float, str | None]:
    parts: list[tuple[float, str]] = []
    for bucket, name, cost in (
        (buckets.tokens_per_minute, "tokens_per_minute", 0.0),
        (buckets.requests_per_minute, "requests_per_minute", 1.0),
        (buckets.tokens_per_day, "tokens_per_day", 0.0),
        (buckets.requests_per_day, "requests_per_day", 1.0),
    ):
        if bucket is not None:
            parts.append((bucket.ready_at(cost), name))
    if buckets.blocked_until is not None:
        parts.append((buckets.blocked_until, f"blocked_{blocked_scope or 'minute'}"))
    if not parts:
        return -math.inf, None
    return max(parts, key=lambda part: (part[0], -_HOLDERS.index(part[1])))


def _gaps(session: Session) -> list[tuple[float, float]]:
    """Each request's gap start and its recorded local work, clamped to the gap."""
    out: list[tuple[float, float]] = []
    previous_task, boundary, local = -1, 0.0, 0.0
    for request in session.requests:
        if request.task != previous_task:
            boundary, local = session.tasks[request.task].start_s, 0.0
            previous_task = request.task
        start = boundary
        gap = max(0.0, request.send_s - start)
        out.append((start, min(local, gap)))
        boundary, local = max(start, request.answer_s), request.local_after_s
    return out


def _replay(session: Session) -> list[_Probe]:
    """Walk the session through the client's buckets and probe each request's gap."""
    clock = _ReplayClock()
    buckets = {pool: ModelBuckets(session.limits, clock) for pool in session.pools}
    blocked_scope: dict[str, str | None] = dict.fromkeys(session.pools)
    gaps = _gaps(session)

    # (time, order, kind, index): at one instant an answer lands before a wall, a wall before a
    # probe, and a probe before the send it is about.
    events: list[tuple[float, int, str, int]] = []
    for index, request in enumerate(session.requests):
        start, local = gaps[index]
        events.append((max(start + local, 0.0), 2, "probe", index))
        events.append((request.send_s, 3, "send", index))
        events.append((request.answer_s, 0, "answer", index))
    for index, wall in enumerate(session.walls):
        events.append((wall.at_s, 1, "wall", index))
    events.sort()

    probes: dict[int, _Probe] = {}
    for time, _, kind, index in events:
        clock.t = max(clock.t, time)
        if kind == "answer":
            request = session.requests[index]
            buckets[request.pool].charge_tokens(request.tokens)
        elif kind == "wall":
            wall = session.walls[index]
            buckets[wall.pool].charge_request()
            buckets[wall.pool].block_for(wall.blocked_for_s, wall.scope)
            blocked_scope[wall.pool] = wall.scope
        elif kind == "probe":
            request = session.requests[index]
            start, local = gaps[index]
            chosen = buckets[request.pool]
            opened, holder = _holder(chosen, blocked_scope[request.pool])
            opened = max(opened, clock.t)
            others = [max(b.ready_at(), clock.t) for p, b in buckets.items() if p != request.pool]
            refusals = [w for w in session.walls if start < w.at_s <= request.send_s]
            scope = None
            if refusals:
                scope = "day" if any(w.scope == "day" for w in refusals) else "minute"
            probes[index] = _Probe(
                gap_start=start,
                local=local,
                remainder=max(0.0, request.send_s - (start + local)),
                opened_after=opened - clock.t,
                holder=holder,
                another_pool_open=any(o - clock.t <= TOLERANCE_S for o in others),
                refusal_scope=scope,
            )
        else:  # send
            buckets[session.requests[index].pool].charge_request()
    return [probes[i] for i in range(len(session.requests))]


def _classify(probe: _Probe, tolerance: float) -> str:
    """Held only when the pool opened during the gap AND within the tolerance of the send.

    The agent finishes its own work before it asks the scheduler, and the scheduler sends the
    moment a pool is open. So a pool that opened well before the send was open while the agent
    was still working, and the gap was work — as far as the replay can see — not waiting.
    """
    if probe.refusal_scope is not None:
        return ACROSS
    if probe.opened_after <= tolerance:
        return NOT_HELD
    send_after_opening = probe.remainder - probe.opened_after
    if send_after_opening > tolerance:
        return NOT_HELD
    if send_after_opening >= -tolerance:
        return HELD
    return "replay_later_than_send"


# -- the split of running time -----------------------------------------------------------------


def _intervals(
    session: Session, kinds: Sequence[str]
) -> tuple[list[tuple[float, float, str]], float]:
    """Every second of the session in one category, and how much overlap had to be clamped."""
    out: list[tuple[float, float, str]] = []
    overlap = 0.0
    cursor = 0.0
    asked = 0.0  # the last boundary asked for, clamped or not

    def take(until: float, category: str) -> None:
        # Overlap is how far the timestamps stepped back, counted once per step: a boundary
        # before the one asked for just before it. Clamping keeps every second counted once.
        nonlocal cursor, overlap, asked
        if until < asked:
            overlap += asked - until
        asked = until
        if until > cursor:
            out.append((cursor, until, category))
            cursor = until

    by_task: dict[int, list[int]] = {}
    for index, request in enumerate(session.requests):
        by_task.setdefault(request.task, []).append(index)

    for task_index, task in enumerate(session.tasks):
        take(min(task.start_s, session.running_s), OUTSIDE)
        local = 0.0
        for index in by_task.get(task_index, []):
            request = session.requests[index]
            gap_end = min(request.send_s, session.running_s)
            take(min(cursor + local, gap_end), LOCAL)
            category = kinds[index]
            take(gap_end, NOT_HELD if category == "replay_later_than_send" else category)
            take(min(request.answer_s, session.running_s), LATENCY)
            local = request.local_after_s
        end = min(task.end_s, session.running_s)
        take(min(cursor + local, end), LOCAL)
        take(end, AFTER)
    take(session.running_s, OUTSIDE)
    return out, overlap


def _split(intervals: Iterable[tuple[float, float, str]]) -> dict[str, float]:
    totals = dict.fromkeys(CATEGORIES, 0.0)
    for start, end, category in intervals:
        totals[category] += end - start
    return totals


# -- the loss ----------------------------------------------------------------------------------


def _loss(
    session: Session,
    ceiling: Mapping[str, Any],
    intervals: Sequence[tuple[float, float, str]],
) -> dict[str, Any]:
    """Running time less the ceiling, in four parts that sum to it exactly.

    For one pool whose per-minute bucket binds throughout, the time before the last send less
    the ceiling's floor is exactly the full-bucket time plus the capacity left in the bucket
    when the last request went out, so ``difference_s`` is zero up to rounding. Pools joining,
    or a day refusal cutting a pool's refill, break that identity, and what is left is stated.
    """
    requests = session.requests
    total_loss = session.running_s - ceiling["ceiling_s"]
    by_category = dict.fromkeys(CATEGORIES, 0.0)
    if not requests:
        return {
            "loss_s": total_loss,
            "full_bucket_s": by_category,
            "unused_at_last_send_s": 0.0,
            "after_last_answer_s": session.running_s,
            "difference_s": 0.0,
        }
    last = requests[-1]
    floor = ceiling["ceiling_s"] - last.latency_s
    before_last_send = last.send_s - floor
    after_last_answer = session.running_s - last.answer_s

    token_admissions, request_admissions = _admissions(session)
    admissions = token_admissions if ceiling["binding"] == "tokens" else request_admissions

    # Replay again, stepping through every breakpoint up to the last send, measuring how long
    # each present pool's binding bucket sat full inside each piece of the split.
    clock = _ReplayClock()
    buckets = {pool: ModelBuckets(session.limits, clock) for pool in session.pools}
    charges: list[tuple[float, int, str, int]] = []
    for index, request in enumerate(requests):
        charges.append((request.send_s, 1, "send", index))
        charges.append((request.answer_s, 0, "answer", index))
    for index, wall in enumerate(session.walls):
        charges.append((wall.at_s, 0, "wall", index))
    charges.sort()

    stop = last.send_s
    breaks = {0.0, stop}
    breaks.update(t for t, _, _, _ in charges if t < stop)
    breaks.update(s for s, _, _ in intervals if s < stop)
    breaks.update(e for _, e, _ in intervals if e < stop)
    for admission in admissions.values():
        for at, _, _ in admission.anchors:
            if at < stop:
                breaks.add(at)
        m, d = admission.minute, admission.day
        if m[1] != d[1] and math.isfinite(m[0]) and math.isfinite(d[0]):
            crossing = (d[0] - m[0]) / (m[1] - d[1])
            if 0 < crossing < stop:
                breaks.add(crossing)
    points = sorted(b for b in breaks if 0.0 <= b <= stop)
    starts = [s for s, _, _ in intervals]

    def category_at(t: float) -> str:
        low, high = 0, len(intervals)
        while low < high:
            mid = (low + high) // 2
            if starts[mid] <= t:
                low = mid + 1
            else:
                high = mid
        return intervals[low - 1][2] if low else OUTSIDE

    cursor = 0
    final_send = len(requests) - 1

    def apply_through(moment: float) -> None:
        nonlocal cursor
        while cursor < len(charges) and charges[cursor][0] <= moment:
            time, _, kind, index = charges[cursor]
            if kind == "send" and index == final_send:
                return
            clock.t = max(clock.t, time)
            if kind == "answer":
                buckets[requests[index].pool].charge_tokens(requests[index].tokens)
            elif kind == "send":
                buckets[requests[index].pool].charge_request()
            else:
                wall = session.walls[index]
                buckets[wall.pool].charge_request()
                buckets[wall.pool].block_for(wall.blocked_for_s, wall.scope)
            cursor += 1

    for a, b in pairwise(points):
        apply_through(a)
        clock.t = max(clock.t, a)
        middle = (a + b) / 2.0
        rate_total = sum(admission.slope(middle) for admission in admissions.values())
        if rate_total <= 0:
            continue
        wasted = 0.0
        for pool, admission in admissions.items():
            name = admission.active(middle)
            if name == "anchor":
                continue
            bucket = _binding_bucket(buckets[pool], ceiling["binding"], name)
            if bucket is None or bucket.refill_per_s <= 0:
                continue
            level = bucket.available
            full_from = a + max(0.0, bucket.capacity - level) / bucket.refill_per_s
            if full_from < b:
                wasted += (b - full_from) * admission.slope(middle) / rate_total
        by_category[category_at(middle)] += wasted

    # What the binding buckets still held when the last request went out, with every other
    # pool's last request given back its forgiven tokens: the ceiling let each pool end one
    # request in deficit, and the run only ended one.
    apply_through(stop)
    clock.t = max(clock.t, stop)
    last_on_pool: dict[str, int] = {}
    for request in requests:
        last_on_pool[request.pool] = request.tokens
    left = 0.0
    for pool, admission in admissions.items():
        name = admission.active(stop)
        if name == "anchor":
            continue
        bucket = _binding_bucket(buckets[pool], ceiling["binding"], name)
        if bucket is None:
            continue
        level = bucket.available
        if ceiling["binding"] == "tokens":
            if pool != last.pool:
                level += last_on_pool.get(pool, 0)
        elif pool == last.pool:
            level -= 1.0
        left += level
    # In seconds, as the ceiling's own admission accrues it, counted back from the last send
    # and never past the session's start: a rate read at one instant would misstate it across
    # the point where the day line overtakes the minute line, or where nothing needed refill.
    unused = stop - _least_time(admissions.values(), _total(admissions.values(), stop) - left)

    explained = sum(by_category.values())
    return {
        "loss_s": total_loss,
        "full_bucket_s": by_category,
        "unused_at_last_send_s": unused,
        "after_last_answer_s": after_last_answer,
        "difference_s": before_last_send - explained - unused,
    }


def _refused_pools(session: Session, ceiling: Mapping[str, Any]) -> dict[str, Any]:
    """A diagnostic for the loss's difference: what the ceiling credited each pool Groq refused
    for the day, against what that pool served, before the refusal and after it, up to the
    session's last send. Tokens, when tokens bind; nothing otherwise."""
    if not session.requests or ceiling["binding"] != "tokens":
        return {}
    tokens, _ = _admissions(session)
    stop = session.requests[-1].send_s
    out: dict[str, Any] = {}
    for pool in session.pools:
        refusals = [w.at_s for w in session.walls if w.pool == pool and w.scope == "day"]
        if not refusals:
            continue
        at = min(refusals)
        served = [(r.answer_s, r.tokens) for r in session.requests if r.pool == pool]
        by_refusal = sum(n for when, n in served if when <= at)
        after = sum(n for when, n in served if at < when <= stop)
        admission = tokens[pool]
        out[pool] = {
            "refused_at_s": _r(at),
            "credited_by_refusal": _r(admission(at)),
            "served_by_refusal": by_refusal,
            "credited_after_refusal": _r(admission(stop) - admission(at)) if stop > at else 0.0,
            "served_after_refusal": after,
        }
    return out


def _binding_bucket(buckets: ModelBuckets, binding: str, line: str) -> TokenBucket | None:
    if binding == "tokens":
        return buckets.tokens_per_minute if line == "minute" else buckets.tokens_per_day
    return buckets.requests_per_minute if line == "minute" else buckets.requests_per_day


# -- one session, one run ----------------------------------------------------------------------


def _r(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


def _percentile(values: Sequence[float], percent: float) -> float | None:
    """Nearest rank: the smallest value with at least ``percent`` of the values at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _spread(values: Sequence[float]) -> dict[str, Any]:
    """Count and quantiles, for the diagnostic that is not a distribution of waits."""
    return {
        "count": len(values),
        "min_s": _r(min(values) if values else None),
        "p5_s": _r(_percentile(values, 5)),
        "p50_s": _r(_percentile(values, 50)),
        "p95_s": _r(_percentile(values, 95)),
        "max_s": _r(max(values) if values else None),
    }


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "zero": sum(1 for v in values if v == 0.0),
        "p50_s": _r(_percentile(values, 50)),
        "p90_s": _r(_percentile(values, 90)),
        "p99_s": _r(_percentile(values, 99)),
        "max_s": _r(max(values) if values else None),
        "total_s": _r(sum(values)),
    }


def compute_session(session: Session) -> dict[str, Any]:
    """Every figure 6.1 reads from one session, before rounding for the page."""
    ceiling = _ceiling(session)
    probes = _replay(session)
    kinds = [_classify(probe, TOLERANCE_S) for probe in probes]
    intervals, overlap = _intervals(session, kinds)
    time = _split(intervals)
    loss = _loss(session, ceiling, intervals)

    waits: list[float] = []
    not_held: list[float] = []
    not_held_open: list[float] = []
    not_held_opened: list[float] = []
    by_holder = dict.fromkeys(_HOLDERS, 0.0)
    by_refusal = {"minute": 0.0, "day": 0.0}
    counts = {HELD: 0, ACROSS: 0, NOT_HELD: 0, "replay_later_than_send": 0}
    for probe, kind in zip(probes, kinds, strict=True):
        counts[kind] += 1
        if kind == HELD:
            waits.append(probe.remainder)
            by_holder[probe.holder or "tokens_per_minute"] += probe.remainder
        elif kind == ACROSS:
            waits.append(probe.remainder)
            by_refusal[probe.refusal_scope or "minute"] += probe.remainder
        else:
            waits.append(0.0)
            not_held.append(probe.remainder)
            if probe.opened_after <= TOLERANCE_S:
                not_held_open.append(probe.remainder)
            else:
                not_held_opened.append(probe.remainder)
    sensitivity = {
        f"{tolerance}": sum(1 for p in probes if _classify(p, tolerance) == HELD)
        for tolerance in SENSITIVITY_S
    }
    send_after_opening = [
        p.remainder - p.opened_after
        for p in probes
        if p.refusal_scope is None and p.opened_after > TOLERANCE_S
    ]
    local = sum(r.local_after_s for r in session.requests)
    return {
        "ceiling": ceiling,
        "steady_s": _steady_s(session),
        "concurrency_1_floor_s": sum(r.latency_s for r in session.requests) + time[LOCAL],
        "recorded_local_work_s": local,
        "time": time,
        "overlap_s": overlap,
        "loss": loss,
        "classification": counts,
        "held_at_other_tolerances": sensitivity,
        "another_pool_open_when_held": sum(
            1 for p, k in zip(probes, kinds, strict=True) if k == HELD and p.another_pool_open
        ),
        "waits": waits,
        "not_held": not_held,
        "wait_by_holder": by_holder,
        "wait_by_refusal": by_refusal,
        "send_after_opening": send_after_opening,
        "not_held_open": not_held_open,
        "not_held_opened": not_held_opened,
        "refused_pools": _refused_pools(session, ceiling),
    }


def _session_figures(session: Session, figures: Mapping[str, Any]) -> dict[str, Any]:
    tasks_complete = sum(1 for t in session.tasks if t.status == "complete")
    tasks_failed = sum(1 for t in session.tasks if t.status != "complete")
    ceiling = {
        key: (_r(value) if isinstance(value, float) else value)
        for key, value in figures["ceiling"].items()
    }
    return {
        "started_at": session.started_at,
        "running_s": _r(session.running_s),
        "run_end_elapsed_s": _r(session.elapsed_s),
        "pools": list(session.pools),
        "tasks_complete": tasks_complete,
        "tasks_failed": tasks_failed,
        "requests": len(session.requests),
        "tokens": sum(r.tokens for r in session.requests),
        "walls": {
            "minute": sum(1 for w in session.walls if w.scope == "minute"),
            "day": sum(1 for w in session.walls if w.scope == "day"),
        },
        "ceiling": ceiling,
        "ratio": _r(
            figures["ceiling"]["ceiling_s"] / session.running_s if session.running_s else None, 6
        ),
        "steady_s": _r(figures["steady_s"]),
        "concurrency_1_floor_s": _r(figures["concurrency_1_floor_s"]),
        "time": {key: _r(value) for key, value in figures["time"].items()},
        "overlap_s": _r(figures["overlap_s"]),
        "loss": _round_loss(figures["loss"]),
        "classification": figures["classification"],
        "refused_pools": figures["refused_pools"],
    }


def _round_loss(loss: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "loss_s": _r(loss["loss_s"]),
        "full_bucket_s": {k: _r(v) for k, v in loss["full_bucket_s"].items()},
        "unused_at_last_send_s": _r(loss["unused_at_last_send_s"]),
        "after_last_answer_s": _r(loss["after_last_answer_s"]),
        "difference_s": _r(loss["difference_s"]),
    }


def compute_run(sessions: Sequence[Session]) -> dict[str, Any]:
    """One run's reading: every session, then the run as the sum of its sessions."""
    if not sessions:
        raise ValueError("a run with no sessions has nothing to read")
    per_session = [(s, compute_session(s)) for s in sessions]
    running = sum(s.running_s for s in sessions)
    ceiling = sum(f["ceiling"]["ceiling_s"] for _, f in per_session)
    steady_parts = [f["steady_s"] for _, f in per_session if f["steady_s"] is not None]
    complete = sum(1 for s in sessions for t in s.tasks if t.status == "complete")

    starts = [datetime.fromisoformat(s.started_at).timestamp() for s in sessions]
    ends = [start + s.running_s for start, s in zip(starts, sessions, strict=True)]
    gaps = [max(0.0, nxt - end) for end, nxt in zip(ends, starts[1:], strict=False)]

    time = dict.fromkeys(CATEGORIES, 0.0)
    full = dict.fromkeys(CATEGORIES, 0.0)
    unused = after = difference = loss = overlap = 0.0
    lateness: list[float] = []
    open_gaps: list[float] = []
    opened_gaps: list[float] = []
    waits: list[float] = []
    not_held: list[float] = []
    holders = dict.fromkeys(_HOLDERS, 0.0)
    refusals = {"minute": 0.0, "day": 0.0}
    counts = {HELD: 0, ACROSS: 0, NOT_HELD: 0, "replay_later_than_send": 0}
    sensitivity: dict[str, int] = {}
    another = 0
    floor_c1 = 0.0
    repeated = {"tasks": 0, "requests": 0, "tokens": 0, "seconds": 0.0}
    for session, figures in per_session:
        for key in CATEGORIES:
            time[key] += figures["time"][key]
            full[key] += figures["loss"]["full_bucket_s"][key]
        unused += figures["loss"]["unused_at_last_send_s"]
        after += figures["loss"]["after_last_answer_s"]
        difference += figures["loss"]["difference_s"]
        lateness += figures["send_after_opening"]
        open_gaps += figures["not_held_open"]
        opened_gaps += figures["not_held_opened"]
        loss += figures["loss"]["loss_s"]
        overlap += figures["overlap_s"]
        waits += figures["waits"]
        not_held += figures["not_held"]
        for key, value in figures["wait_by_holder"].items():
            holders[key] += value
        for key, value in figures["wait_by_refusal"].items():
            refusals[key] += value
        for key, value in figures["classification"].items():
            counts[key] += value
        for key, value in figures["held_at_other_tolerances"].items():
            sensitivity[key] = sensitivity.get(key, 0) + value
        another += figures["another_pool_open_when_held"]
        floor_c1 += figures["concurrency_1_floor_s"]
        for index, task in enumerate(session.tasks):
            if task.status == "complete":
                continue
            repeated["tasks"] += 1
            repeated["seconds"] += task.end_s - task.start_s
            for request in session.requests:
                if request.task == index:
                    repeated["requests"] += 1
                    repeated["tokens"] += request.tokens

    minutes = running / 60.0
    return {
        "sessions": [_session_figures(s, f) for s, f in per_session],
        "summary": {
            "sessions": len(sessions),
            "tasks_complete": complete,
            "requests": sum(len(s.requests) for s in sessions),
            "tokens": sum(r.tokens for s in sessions for r in s.requests),
            "running_s": _r(running),
            "wall_clock_s": _r(ends[-1] - starts[0]),
            "operator_gaps_s": _r(sum(gaps)),
            "ceiling_s": _r(ceiling),
            "ratio": _r(ceiling / running if running else None, 6),
            "ratio_percent": _r(100.0 * ceiling / running if running else None),
            "achieved_tasks_per_minute": _r(complete / minutes if minutes else None),
            "ceiling_tasks_per_minute": _r(complete / (ceiling / 60.0) if ceiling else None),
            "steady_s": _r(sum(steady_parts) if steady_parts else None),
            "steady_ratio": _r(
                sum(steady_parts) / running if steady_parts and running else None, 6
            ),
            "concurrency_1_floor_s": _r(floor_c1),
            "time": {key: _r(value) for key, value in time.items()},
            "overlap_s": _r(overlap),
            "loss": {
                "loss_s": _r(loss),
                "full_bucket_s": {key: _r(value) for key, value in full.items()},
                "unused_at_last_send_s": _r(unused),
                "after_last_answer_s": _r(after),
                "difference_s": _r(difference),
            },
            "queue_wait": {
                **_distribution(waits),
                "by_holder_s": {key: _r(value) for key, value in holders.items()},
                "across_a_refusal_s": {key: _r(value) for key, value in refusals.items()},
                "classification": counts,
                "held_at_other_tolerances": sensitivity,
                "another_pool_open_when_held": another,
            },
            "not_held_gaps": _distribution(not_held),
            "opened_during_gap": {
                "send_after_opening": _spread(lateness),
                "not_held_pool_open_when_work_ended": _distribution(open_gaps),
                "not_held_pool_opened_during_gap": _distribution(opened_gaps),
            },
            "repeated_work": {**repeated, "seconds": _r(repeated["seconds"])},
            "walls": {
                "minute": sum(1 for s in sessions for w in s.walls if w.scope == "minute"),
                "day": sum(1 for s in sessions for w in s.walls if w.scope == "day"),
            },
        },
    }


# -- to and from the committed file -------------------------------------------------------------


def session_to_document(session: Session) -> dict[str, Any]:
    """The inputs as the committed file carries them: compact rows, pools by index."""
    index = {pool: i for i, pool in enumerate(session.pools)}
    return {
        "started_at": session.started_at,
        "running_s": session.running_s,
        "run_end_elapsed_s": session.elapsed_s,
        "pools": list(session.pools),
        "tasks": [[t.start_s, t.end_s, t.status] for t in session.tasks],
        "requests": [
            [r.send_s, r.latency_s, r.tokens, index[r.pool], r.task, r.local_after_s]
            for r in session.requests
        ],
        "walls": [
            [w.at_s, index[w.pool], w.scope, w.blocked_for_s, w.quota_id, w.limit, w.used]
            for w in session.walls
        ],
    }


def session_from_document(document: Mapping[str, Any], limits: Limits) -> Session:
    pools = tuple(document["pools"])
    return Session(
        started_at=document["started_at"],
        running_s=document["running_s"],
        elapsed_s=document.get("run_end_elapsed_s"),
        pools=pools,
        limits=limits,
        tasks=tuple(TaskSpan(start, end, status) for start, end, status in document["tasks"]),
        requests=tuple(
            Request(send, latency, tokens, pools[pool], task, local)
            for send, latency, tokens, pool, task, local in document["requests"]
        ),
        walls=tuple(
            Wall(at, pools[pool], scope, blocked, quota_id, limit, used)
            for at, pool, scope, blocked, quota_id, limit, used in document["walls"]
        ),
    )


# -- from a ledger -------------------------------------------------------------------------------

_LIMIT_USED = re.compile(r"Limit (\d+), Used (\d+)")


def _seconds(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def _pool_order(pool: str) -> tuple[str, int]:
    name, _, number = pool.partition("#")
    return (name, int(number) if number.isdigit() else 0)


def sessions_from_ledger(
    rows: Iterable[Mapping[str, Any]],
    walls: Iterable[Mapping[str, Any]],
    *,
    limits_for: Callable[[str], Limits],
    local_after: Mapping[tuple[str, str], float] | None = None,
) -> list[Session]:
    """A run's sessions, from its ledger rows in file order and the quota-wall log.

    A task's attempts are the attempt rows between its row and the previous task row, which
    holds at concurrency 1 and is checked: every attempt names the task that follows it. A wall
    belongs to a session when it was observed inside the session and names the run's model;
    ``runs/quota-walls.jsonl`` carries no run id. ``local_after`` maps ``(task_id, the attempt's
    recorded_at)`` to the recorded tool time of that attempt's turn.
    """
    local_after = local_after or {}
    rows = list(rows)
    wall_rows = list(walls)
    # The run's model, from every attempt it made: a session that answered nothing still owns the
    # walls it met on that model.
    models = {str(row["model"]) for row in rows if row.get("kind") == "attempt"}
    if len(models) != 1:
        raise ValueError(f"a run served by {len(models)} models cannot be read as one: {models}")
    (model,) = models
    limits = limits_for(model)
    sessions: list[Session] = []
    start: str | None = None
    tasks: list[TaskSpan] = []
    requests: list[tuple[Mapping[str, Any], int]] = []
    unclaimed: list[Mapping[str, Any]] = []
    concurrency: Any = None

    def rel(stamp: str) -> float:
        assert start is not None
        return round(_seconds(stamp) - _seconds(start), 6)

    for row in rows:
        kind = row.get("kind")
        if kind == "run_start":
            if start is not None:
                raise ValueError(f"run_start at {row['started_at']} inside an open session")
            start, tasks, requests, unclaimed = str(row["started_at"]), [], [], []
            concurrency = (row.get("declared") or {}).get("concurrency")
            if concurrency != 1:
                raise ValueError(f"session {start} declared concurrency {concurrency}, not 1")
        elif kind == "attempt":
            if start is None:
                raise ValueError("an attempt row outside any session")
            if row.get("latency_s") is None:
                raise ValueError(f"attempt at {row['recorded_at']} has no latency")
            unclaimed.append(row)
        elif kind == "task":
            if start is None:
                raise ValueError("a task row outside any session")
            for attempt in unclaimed:
                if attempt["task_id"] != row["task_id"]:
                    raise ValueError(
                        f"attempt for {attempt['task_id']} sits before {row['task_id']}'s row"
                    )
            end = rel(str(row["recorded_at"]))
            tasks.append(TaskSpan(round(end - float(row["elapsed_s"]), 6), end, str(row["status"])))
            requests.extend((attempt, len(tasks) - 1) for attempt in unclaimed)
            unclaimed = []
        elif kind == "run_end":
            if start is None:
                raise ValueError("run_end with no session open")
            if unclaimed:
                raise ValueError(f"session {start} ends with attempts that no task row claims")
            running = rel(str(row["ended_at"]))
            began, finished = _seconds(start), _seconds(str(row["ended_at"]))
            inside = [
                w
                for w in wall_rows
                if began <= _seconds(str(w["observed_at"])) <= finished and w["model"] == model
            ]
            pools = sorted(
                {str(a["pool"]) for a, _ in requests} | {str(w["pool"]) for w in inside},
                key=_pool_order,
            )
            built: list[Request] = []
            for attempt, task in requests:
                latency = round(float(attempt["latency_s"]), 6)
                answer = rel(str(attempt["recorded_at"]))
                local = local_after.get((str(attempt["task_id"]), str(attempt["recorded_at"])), 0.0)
                built.append(
                    Request(
                        send_s=round(answer - latency, 6),
                        latency_s=latency,
                        tokens=int(attempt.get("prompt_tokens") or 0)
                        + int(attempt.get("completion_tokens") or 0),
                        pool=str(attempt["pool"]),
                        task=task,
                        local_after_s=round(local, 6),
                    )
                )
            built_walls: list[Wall] = []
            for wall in sorted(inside, key=lambda w: str(w["observed_at"])):
                match = _LIMIT_USED.search(str(wall.get("body") or ""))
                built_walls.append(
                    Wall(
                        at_s=rel(str(wall["observed_at"])),
                        pool=str(wall["pool"]),
                        scope=str(wall["scope"]),
                        blocked_for_s=float(wall["blocked_for_s"]),
                        quota_id=wall.get("quota_id"),
                        limit=int(match.group(1)) if match else None,
                        used=int(match.group(2)) if match else None,
                    )
                )
            sessions.append(
                Session(
                    started_at=start,
                    running_s=running,
                    elapsed_s=round(float(row.get("elapsed_s") or 0.0), 6),
                    pools=tuple(pools),
                    limits=limits,
                    tasks=tuple(tasks),
                    requests=tuple(sorted(built, key=lambda r: r.send_s)),
                    walls=tuple(built_walls),
                )
            )
            start = None
    if start is not None:
        raise ValueError(f"session {start} has no run_end: the ledger was cut off")
    return sessions


# -- the committed file ---------------------------------------------------------------------------

RUN_META: Final = ("agent", "run_id", "ledger", "provider", "model", "date", "transcripts")


def limits_to_document(limits: Limits) -> dict[str, int | None]:
    return {"rpm": limits.rpm, "rpd": limits.rpd, "tpm": limits.tpm, "tpd": limits.tpd}


def limits_from_document(document: Mapping[str, Any]) -> Limits:
    return Limits(
        rpm=document["rpm"], rpd=document["rpd"], tpm=document["tpm"], tpd=document["tpd"]
    )


def run_document(meta: Mapping[str, Any], sessions: Sequence[Session]) -> dict[str, Any]:
    """One run as the file holds it: who and where, the inputs, then every figure."""
    unknown = set(meta) - set(RUN_META)
    if unknown:
        raise ValueError(f"run metadata the file does not carry: {sorted(unknown)}")
    return {
        **{key: meta.get(key) for key in RUN_META},
        "inputs": [session_to_document(s) for s in sessions],
        **compute_run(sessions),
    }


def document(
    measurement: Mapping[str, Any],
    limits: Mapping[str, Limits],
    runs: Sequence[tuple[Mapping[str, Any], Sequence[Session]]],
) -> dict[str, Any]:
    return {
        "measurement": dict(measurement),
        "definitions": dict(DEFINITIONS),
        "limits": {model: limits_to_document(value) for model, value in limits.items()},
        "runs": [run_document(meta, sessions) for meta, sessions in runs],
    }


def regenerate(committed: Mapping[str, Any]) -> dict[str, Any]:
    """The committed file recomputed from the inputs it carries, and nothing else."""
    limits = {model: limits_from_document(v) for model, v in committed["limits"].items()}
    runs = []
    for run in committed["runs"]:
        sessions = [session_from_document(s, limits[run["model"]]) for s in run["inputs"]]
        runs.append(({key: run[key] for key in RUN_META}, sessions))
    return document(committed["measurement"], limits, runs)


def dumps(value: Any, indent: int = 0) -> str:
    """JSON with two-space indentation, except that a list of scalars sits on one line.

    The inputs are some two thousand rows of six numbers; one number to a line would make the
    file unreadable and every diff of it unreviewable. Deterministic, so the committed text is
    what a regeneration writes.
    """
    pad, inner = "  " * indent, "  " * (indent + 1)
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        items = [f"{inner}{json.dumps(str(k))}: {dumps(v, indent + 1)}" for k, v in value.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(value, list | tuple):
        if not value:
            return "[]"
        if all(not isinstance(v, Mapping | list | tuple) for v in value):
            return "[" + ", ".join(json.dumps(v) for v in value) + "]"
        items = [f"{inner}{dumps(v, indent + 1)}" for v in value]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    return json.dumps(value)
