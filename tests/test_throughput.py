"""6.1: scheduler efficiency — the ceiling, the replay, the split of running time, the loss.

Hand-built sessions pin every clause of `docs/PERFORMANCE.md` to an exact figure, and the
committed `results/scheduler-efficiency.json` is recomputed from the inputs it carries — the
ledgers it was built from are gitignored, so this is the only way CI sees the arithmetic.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from query_pilot.client.config import Limits
from query_pilot.run import throughput as t
from query_pilot.run.throughput import Request, Session, TaskSpan, Wall

REPO = Path(__file__).resolve().parent.parent
COMMITTED = REPO / "results" / "scheduler-efficiency.json"

# Ten tokens a second on the per-minute bucket; nothing else binds unless a test says so.
LIMITS = Limits(rpm=1_000_000, rpd=1_000_000_000, tpm=600, tpd=1_000_000_000)


def session(
    requests: list[Request],
    *,
    tasks: list[TaskSpan] | None = None,
    walls: list[Wall] | None = None,
    pools: tuple[str, ...] = ("p",),
    running: float = 100.0,
    limits: Limits = LIMITS,
) -> Session:
    return Session(
        started_at="2026-09-12T00:00:00+00:00",
        running_s=running,
        pools=pools,
        limits=limits,
        tasks=tuple(tasks or [TaskSpan(0.0, running, "complete")]),
        requests=tuple(requests),
        walls=tuple(walls or []),
    )


def req(send: float, tokens: int, *, latency: float = 1.0, pool: str = "p", task: int = 0,
        local: float = 0.0) -> Request:  # fmt: skip
    return Request(send, latency, tokens, pool, task, local)


# -- the ceiling -------------------------------------------------------------------------------


def test_the_ceiling_starts_full_and_forgives_each_pools_last_request() -> None:
    # 1,200 tokens; the last 300 forgiven; the first 600 are the full bucket; 300 more at 10/s.
    s = session([req(0, 300), req(2, 300), req(4, 300), req(6, 300)])
    ceiling = t.compute_session(s)["ceiling"]
    assert ceiling["tokens_floor_s"] == pytest.approx(30.0)
    assert ceiling["binding"] == "tokens"
    assert ceiling["ceiling_s"] == pytest.approx(31.0)  # plus the last request's latency


def test_the_ceiling_is_the_later_of_the_request_and_token_floors() -> None:
    limits = Limits(rpm=2, rpd=1_000_000, tpm=600, tpd=1_000_000_000)
    s = session([req(i * 10.0, 10) for i in range(5)], limits=limits)
    ceiling = t.compute_session(s)["ceiling"]
    # Five requests against a bucket of two refilling at one every 30 s: the fifth at 90 s.
    assert ceiling["requests_floor_s"] == pytest.approx(90.0)
    assert ceiling["tokens_floor_s"] == 0.0
    assert ceiling["binding"] == "requests"
    assert ceiling["ceiling_s"] == pytest.approx(91.0)


def test_capacity_is_spread_over_the_pools_present_in_any_proportion() -> None:
    one = session([req(i * 2.0, 300) for i in range(6)])
    two = session(
        [req(i * 2.0, 300, pool="a" if i % 2 else "b") for i in range(6)], pools=("a", "b")
    )
    # One pool: 1,800 less its last 300 less its full 600, at 10/s. Two: both full buckets
    # and both last requests forgiven cover everything at once.
    assert t.compute_session(one)["ceiling"]["tokens_floor_s"] == pytest.approx(90.0)
    assert t.compute_session(two)["ceiling"]["tokens_floor_s"] == 0.0


def test_a_day_refusal_is_taken_as_the_providers_own_count() -> None:
    limits = Limits(rpm=1_000_000, rpd=1_000_000_000, tpm=600, tpd=100_000)
    requests = [req(0, 400), req(2, 400), req(4, 400), req(6, 400)]
    free = t.compute_session(session(requests, limits=limits))["ceiling"]
    assert free["tokens_floor_s"] == pytest.approx(60.0)
    refused = session(
        requests,
        limits=limits,
        walls=[Wall(10.0, "p", "day", 3600.0, "TPD", limit=100_000, used=99_900)],
    )
    # From the refusal on: what the ceiling had admitted by then (700), the 100 the refusal
    # says is left, and the day's refill of 100,000 per 86,400 s.
    floor = t.compute_session(refused)["ceiling"]["tokens_floor_s"]
    assert floor == pytest.approx(10.0 + 400.0 / (100_000 / 86_400.0), rel=1e-9)


def test_a_minute_refusal_does_not_move_the_ceiling() -> None:
    requests = [req(0, 300), req(2, 300), req(4, 300), req(6, 300)]
    with_wall = session(requests, walls=[Wall(3.0, "p", "minute", 2.0, "TPM", 600, 590)])
    assert (
        t.compute_session(with_wall)["ceiling"] == t.compute_session(session(requests))["ceiling"]
    )


def test_a_day_refusal_naming_an_unmodelled_quota_is_refused() -> None:
    s = session([req(0, 300)], walls=[Wall(1.0, "p", "day", 3600.0, "XYZ", None, None)])
    with pytest.raises(ValueError, match="not modelled"):
        t.compute_session(s)


def test_the_steady_rate_is_not_a_ceiling() -> None:
    s = session([req(i * 1.5, 300) for i in range(4)], running=8.0)
    figures = t.compute_session(s)
    # 1,200 tokens at 10/s is 120 s; the full bucket and the forgiven last make it 31.
    assert figures["steady_s"] == pytest.approx(120.0)
    assert figures["ceiling"]["ceiling_s"] < figures["steady_s"]


def test_a_session_with_no_answered_request_has_a_zero_ceiling_and_loses_all_of_it() -> None:
    s = session([], tasks=[TaskSpan(0.5, 4.5, "failed")], running=5.0)
    figures = t.compute_session(s)
    assert figures["ceiling"]["ceiling_s"] == 0.0
    assert figures["loss"]["loss_s"] == pytest.approx(5.0)
    assert figures["loss"]["after_last_answer_s"] == pytest.approx(5.0)
    assert figures["loss"]["difference_s"] == 0.0


# -- the replay and what it calls held -----------------------------------------------------------

# One request of 1,200 tokens answered at 1 s leaves the bucket at -600: it opens at 61 s.
# The second request's recorded local work ends at 1.5 s, which is when the replay looks.


def held_case(second_send: float, *, walls: list[Wall] | None = None) -> dict:
    return t.compute_session(
        session(
            [req(0, 1_200, local=0.5), req(second_send, 100)],
            tasks=[TaskSpan(0.0, second_send + 2.0, "complete")],
            walls=walls,
            running=second_send + 3.0,
        )
    )


def test_a_request_that_leaves_as_its_pool_opens_was_held() -> None:
    figures = held_case(61.04)
    assert figures["classification"][t.HELD] == 1
    assert figures["waits"][1] == pytest.approx(61.04 - 1.5)  # the gap less recorded work
    assert figures["time"][t.HELD] == pytest.approx(61.04 - 1.5)
    assert figures["time"][t.LOCAL] == pytest.approx(0.5)
    assert figures["wait_by_holder"]["tokens_per_minute"] == pytest.approx(61.04 - 1.5)


def test_the_tolerance_admits_004_s_after_the_opening_and_rejects_006() -> None:
    assert held_case(61.04)["classification"][t.HELD] == 1
    assert held_case(61.06)["classification"][t.NOT_HELD] == 2


def test_a_pool_that_opened_well_before_the_send_did_not_hold_it() -> None:
    # Open at 61, sent at 70: the agent was still working at 61, so the gap was work.
    figures = held_case(70.0)
    assert figures["classification"][t.HELD] == 0
    assert figures["waits"][1] == 0.0
    assert figures["time"][t.NOT_HELD] == pytest.approx(70.0 - 1.5)
    assert figures["send_after_opening"] == [pytest.approx(70.0 - 61.0)]
    assert figures["not_held_opened"] == [pytest.approx(70.0 - 1.5)]


def test_a_send_the_replay_says_came_before_its_pool_opened_is_counted_and_not_held() -> None:
    figures = held_case(30.0)
    assert figures["classification"]["replay_later_than_send"] == 1
    assert figures["time"][t.NOT_HELD] == pytest.approx(30.0 - 1.5)


def test_a_refusal_inside_the_gap_makes_it_a_wait_across_a_refusal() -> None:
    figures = held_case(70.0, walls=[Wall(20.0, "p", "minute", 2.0, "TPM", 600, 590)])
    assert figures["classification"][t.ACROSS] == 1
    assert figures["waits"][1] == pytest.approx(70.0 - 1.5)
    assert figures["wait_by_refusal"]["minute"] == pytest.approx(70.0 - 1.5)


def test_classifications_are_reported_at_the_other_tolerances() -> None:
    figures = held_case(61.1)  # 0.1 s after the opening
    assert figures["classification"][t.HELD] == 0
    assert figures["held_at_other_tolerances"] == {"0.01": 0, "0.2": 1}


# -- every second once ---------------------------------------------------------------------------


def test_every_second_of_a_session_falls_in_exactly_one_category() -> None:
    s = session(
        [req(3.0, 1_200, local=0.5), req(64.02, 100, local=0.2)],
        tasks=[TaskSpan(2.0, 70.0, "complete")],
        running=80.0,
    )
    time = t.compute_session(s)["time"]
    assert sum(time.values()) == pytest.approx(80.0, abs=1e-9)
    assert time[t.OUTSIDE] == pytest.approx(2.0 + 10.0)
    assert time[t.LATENCY] == pytest.approx(2.0)
    assert time[t.LOCAL] == pytest.approx(0.5 + 0.2)
    assert time[t.NOT_HELD] == pytest.approx(1.0)  # the first gap: the bucket was full
    assert time[t.HELD] == pytest.approx(64.02 - 4.5)  # answered at 4, open again at 64
    assert time[t.AFTER] == pytest.approx(70.0 - 65.02 - 0.2)


def test_overlapping_timestamps_are_clamped_and_the_overlap_reported() -> None:
    # A send stamped before its task began.
    s = session([req(1.0, 100)], tasks=[TaskSpan(1.5, 5.0, "complete")], running=6.0)
    figures = t.compute_session(s)
    assert figures["overlap_s"] == pytest.approx(0.5)
    assert sum(figures["time"].values()) == pytest.approx(6.0, abs=1e-9)


# -- the loss --------------------------------------------------------------------------------------


def test_one_pool_the_loss_is_full_bucket_time_plus_unused_capacity_plus_the_tail() -> None:
    # Bucket full from 0 until the first answer at 1; the second request goes out 2 s after
    # its pool reopens at 61, as a client holding a lowered bucket would send it.
    s = session(
        [req(0.0, 1_200), req(63.0, 300)],
        tasks=[TaskSpan(0.0, 65.0, "complete")],
        running=70.0,
    )
    figures = t.compute_session(s)
    loss = figures["loss"]
    assert figures["ceiling"]["ceiling_s"] == pytest.approx(61.0)
    assert loss["loss_s"] == pytest.approx(9.0)
    assert loss["full_bucket_s"][t.LATENCY] == pytest.approx(1.0)
    assert loss["unused_at_last_send_s"] == pytest.approx(2.0)
    assert loss["after_last_answer_s"] == pytest.approx(6.0)
    assert loss["difference_s"] == pytest.approx(0.0, abs=1e-6)


def test_the_loss_parts_always_sum_to_the_loss() -> None:
    s = session(
        [req(0.0, 1_200, pool="a"), req(5.0, 900, pool="b"), req(70.0, 300, pool="a")],
        pools=("a", "b"),
        walls=[Wall(30.0, "b", "day", 3600.0, "TPD", 1_000_000_000, 999_999_950)],
        tasks=[TaskSpan(0.0, 72.0, "complete")],
        running=75.0,
    )
    loss = t.compute_session(s)["loss"]
    parts = (
        sum(loss["full_bucket_s"].values())
        + loss["unused_at_last_send_s"]
        + loss["after_last_answer_s"]
        + loss["difference_s"]
    )
    assert parts == pytest.approx(loss["loss_s"], abs=1e-9)


def test_a_refused_pool_is_described_before_and_after_its_refusal() -> None:
    s = session(
        [req(0.0, 400, pool="a"), req(2.0, 400, pool="b"), req(40.0, 400, pool="b")],
        pools=("a", "b"),
        walls=[Wall(5.0, "a", "day", 3600.0, "TPD", 1_000_000_000, 999_999_000)],
        tasks=[TaskSpan(0.0, 42.0, "complete")],
        running=45.0,
    )
    refused = t.compute_session(s)["refused_pools"]
    assert set(refused) == {"a"}
    assert refused["a"]["served_by_refusal"] == 400
    assert refused["a"]["served_after_refusal"] == 0
    assert refused["a"]["credited_by_refusal"] == pytest.approx(650.0)  # 600 full + 5 s at 10/s


# -- distributions -------------------------------------------------------------------------------


def test_percentiles_are_nearest_rank() -> None:
    values = [4.0, 1.0, 3.0, 2.0]
    assert t._percentile(values, 50) == 2.0
    assert t._percentile(values, 90) == 4.0
    assert t._percentile(values, 1) == 1.0
    assert t._percentile([], 50) is None


# -- from a ledger ---------------------------------------------------------------------------------


def ledger_rows(*, concurrency: int = 1, model: str = "m") -> list[dict]:
    return [
        {"kind": "run_start", "started_at": "2026-09-12T00:00:00+00:00",
         "declared": {"concurrency": concurrency}},
        {"kind": "attempt", "task_id": "t1", "pool": "g#1", "model": model, "provider": "groq",
         "recorded_at": "2026-09-12T00:00:02+00:00", "latency_s": 1.0, "prompt_tokens": 500,
         "completion_tokens": 100, "turn": 1},
        {"kind": "attempt", "task_id": "t1", "pool": "g#2", "model": model, "provider": "groq",
         "recorded_at": "2026-09-12T00:00:05+00:00", "latency_s": 0.5, "prompt_tokens": 700,
         "completion_tokens": 50, "turn": 2},
        {"kind": "task", "task_id": "t1", "status": "complete",
         "recorded_at": "2026-09-12T00:00:06+00:00", "elapsed_s": 5.5},
        {"kind": "run_end", "ended_at": "2026-09-12T00:00:10+00:00", "elapsed_s": 10.0},
    ]  # fmt: skip


WALLS = [
    {"observed_at": "2026-09-12T00:00:03+00:00", "pool": "g#3", "model": "m", "scope": "minute",
     "blocked_for_s": 2.0, "quota_id": "TPM", "body": "TPM: Limit 8000, Used 7900, Requested 600"},
    {"observed_at": "2026-09-12T00:00:04+00:00", "pool": "g#1", "model": "other",
     "scope": "minute", "blocked_for_s": 1.0, "quota_id": "TPM", "body": ""},
    {"observed_at": "2026-09-12T00:01:00+00:00", "pool": "g#1", "model": "m", "scope": "day",
     "blocked_for_s": 3600.0, "quota_id": "TPD", "body": ""},
]  # fmt: skip


def test_sessions_are_built_from_ledger_rows_and_the_wall_log() -> None:
    local = {("t1", "2026-09-12T00:00:02+00:00"): 0.25}
    (s,) = t.sessions_from_ledger(
        ledger_rows(), WALLS, limits_for=lambda model: LIMITS, local_after=local
    )
    assert s.running_s == 10.0
    assert s.elapsed_s == 10.0
    assert s.tasks == (TaskSpan(0.5, 6.0, "complete"),)
    assert [(r.send_s, r.latency_s, r.tokens, r.pool, r.local_after_s) for r in s.requests] == [
        (1.0, 1.0, 600, "g#1", 0.25),
        (4.5, 0.5, 750, "g#2", 0.0),
    ]
    # Only the wall inside the session and on the run's model; its pool is present.
    assert s.walls == (Wall(3.0, "g#3", "minute", 2.0, "TPM", 8000, 7900),)
    assert s.pools == ("g#1", "g#2", "g#3")


def test_a_ledger_declaring_more_than_one_request_in_flight_is_refused() -> None:
    with pytest.raises(ValueError, match="concurrency 2"):
        t.sessions_from_ledger(ledger_rows(concurrency=2), [], limits_for=lambda m: LIMITS)


def test_an_attempt_the_next_task_row_does_not_claim_is_refused() -> None:
    rows = ledger_rows()
    rows[1] = {**rows[1], "task_id": "t0"}
    with pytest.raises(ValueError, match="sits before"):
        t.sessions_from_ledger(rows, [], limits_for=lambda m: LIMITS)


def test_a_ledger_cut_off_before_run_end_is_refused() -> None:
    with pytest.raises(ValueError, match="no run_end"):
        t.sessions_from_ledger(ledger_rows()[:-1], [], limits_for=lambda m: LIMITS)


def test_a_run_served_by_two_models_is_refused() -> None:
    rows = ledger_rows()
    rows[2] = {**rows[2], "model": "other"}
    with pytest.raises(ValueError, match="2 models"):
        t.sessions_from_ledger(rows, [], limits_for=lambda m: LIMITS)


def test_an_attempt_without_latency_is_refused() -> None:
    rows = ledger_rows()
    rows[1] = {**rows[1], "latency_s": None}
    with pytest.raises(ValueError, match="no latency"):
        t.sessions_from_ledger(rows, [], limits_for=lambda m: LIMITS)


def test_a_session_round_trips_through_the_committed_shape() -> None:
    (s,) = t.sessions_from_ledger(ledger_rows(), WALLS, limits_for=lambda m: LIMITS)
    assert t.session_from_document(t.session_to_document(s), LIMITS) == s


def test_dumps_puts_scalar_lists_on_one_line_and_parses_back() -> None:
    value = {"a": [1, 2.5, None, "x"], "b": [[1, 2], [3, 4]], "c": {}, "d": []}
    text = t.dumps(value)
    assert '"a": [1, 2.5, null, "x"]' in text
    assert "    [1, 2],\n" in text
    assert json.loads(text) == value


# -- the committed file --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def committed() -> dict:
    return json.loads(COMMITTED.read_text())


def test_the_committed_file_regenerates_exactly_from_the_inputs_it_carries(committed) -> None:
    regenerated = t.regenerate(committed)
    assert regenerated == committed
    assert t.dumps(regenerated) + "\n" == COMMITTED.read_text()


def test_the_committed_definitions_are_the_modules(committed) -> None:
    assert committed["definitions"] == dict(t.DEFINITIONS)
    assert committed["measurement"]["tolerance_s"] == t.TOLERANCE_S
    assert committed["measurement"]["sensitivity_s"] == list(t.SENSITIVITY_S)


def test_the_committed_limits_are_the_declared_ones(committed) -> None:
    raw = tomllib.loads((REPO / "config" / "providers.toml").read_text())
    declared = {
        entry["model"]: {key: entry.get(key) for key in ("rpm", "rpd", "tpm", "tpd")}
        for entry in raw["endpoints"].values()
        if entry["model"] in committed["limits"]
    }
    assert committed["limits"] == declared


def by_agent(committed: dict) -> dict[str, dict]:
    return {run["agent"]: run for run in committed["runs"]}


def test_the_headline_is_a1_and_the_three_ratios_are_pinned(committed) -> None:
    runs = by_agent(committed)
    assert committed["measurement"]["headline"] == runs["A1"]["run_id"] == "20260910-024454-1f69bc"
    pinned = {
        "A1": (0.967871, 5244.1274, 5418.2073, 1.6611, 1.7162),
        "A0": (0.996012, 734.4157, 737.3566, 12.2058, 12.2546),
        "A2-cheap": (0.947392, 3084.1555, 3255.4176, 2.7646, 2.9181),
    }
    for agent, (ratio, ceiling, running, achieved, ceiling_rate) in pinned.items():
        summary = runs[agent]["summary"]
        assert summary["ratio"] == ratio
        assert summary["ceiling_s"] == ceiling
        assert summary["running_s"] == running
        assert summary["achieved_tasks_per_minute"] == achieved
        assert summary["ceiling_tasks_per_minute"] == ceiling_rate
        assert summary["ratio"] <= 1.0, "a run beat its ceiling: the ceiling is wrong"


@pytest.mark.parametrize(
    ("agent", "projection", "sessions", "walls"),
    [
        ("A1", "a1-working.json", 6, {"minute": 8, "day": 4}),
        ("A0", "a0-working.json", 1, {"minute": 2, "day": 0}),
        ("A2-cheap", "a2-cheap-working.json", 6, {"minute": 80, "day": 4}),
    ],
)
def test_each_run_agrees_with_its_committed_projection(
    committed, agent, projection, sessions, walls
) -> None:
    run = by_agent(committed)[agent]
    measured = json.loads((REPO / "results" / projection).read_text())
    summary = run["summary"]
    assert run["run_id"] == measured["measurement"]["run_id"]
    assert run["ledger"] == measured["measurement"]["ledger"]
    assert summary["requests"] == measured["run"]["attempts"]
    assert summary["tokens"] == measured["tokens"]["total"]
    assert summary["tasks_complete"] == measured["run"]["tasks_complete"] == 150
    assert summary["sessions"] == sessions
    assert summary["walls"] == walls


def test_every_session_splits_its_running_time_and_its_loss_exactly(committed) -> None:
    for run in committed["runs"]:
        for s in run["sessions"]:
            assert sum(s["time"].values()) == pytest.approx(s["running_s"], abs=5e-4)
            loss = s["loss"]
            parts = (
                sum(loss["full_bucket_s"].values())
                + loss["unused_at_last_send_s"]
                + loss["after_last_answer_s"]
                + loss["difference_s"]
            )
            assert parts == pytest.approx(loss["loss_s"], abs=5e-4)
            assert s["running_s"] == pytest.approx(s["run_end_elapsed_s"], abs=0.01)
            assert s["overlap_s"] == 0.0


def test_the_difference_lives_only_where_a_day_refusal_cut_one_of_several_pools(committed) -> None:
    for run in committed["runs"]:
        for s in run["sessions"]:
            refused_one_of_several = len(s["pools"]) > 1 and s["walls"]["day"] > 0
            if not refused_one_of_several:
                assert abs(s["loss"]["difference_s"]) < 1.0, (run["agent"], s["started_at"])


def test_nothing_that_builds_the_reading_opens_a_split() -> None:
    # The reserve set is untouchable (constraint 1); this reading needs no split at all.
    for path in ("src/query_pilot/run/throughput.py", "scripts/scheduler_efficiency.py"):
        assert "splits" not in (REPO / path).read_text()
