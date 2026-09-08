"""One fault, driven the whole way: a recorded refusal to a finished run's ledger.

Every other test in this project stops at a package boundary, and for good reason — the
client knows nothing about runs and the run package passes it a stub. But the roadmap's
last client-fault case is *daily-quota exhaustion mid-run*, and mid-run is not a thing
either package can see on its own. 1.2 proved the client raises
:class:`AllPoolsExhausted`; 1.4 proved a run that catches one ends `incomplete` with the
reason ``pools_exhausted``. **Nothing joined them**, and the join is where a mistake would
actually live: the exception raised on one side has to be the exception recognised on the
other, and the refusal that caused it has to reach the ledger with the quota's own name
still attached.

So this file crosses the seam once, deliberately, and **in the direction the dependency
already points** — ``query_pilot.run`` imports ``query_pilot.client`` and never the
reverse. Nothing here weakens that: the client is still handed a stub HTTP layer and a
stub clock, and it still learns nothing about tasks.

**No live call, no key, no socket, and no wall-clock time.** The refusal is the body
`gemini-3.8-flash` returned on 2026-09-06, verbatim.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import GOOGLE_DAILY_429, FakeClock, FakeHttp, response
from query_pilot.client.client import Client
from query_pilot.client.config import ClientConfig
from query_pilot.client.registry import Registry
from query_pilot.client.scheduler import AllPoolsExhausted
from query_pilot.client.types import Message
from query_pilot.run import (
    STATUS_INCOMPLETE,
    IncompleteReason,
    Run,
    RunConfig,
    RunLedger,
    read_rows,
)

ALL_KEYS = {"GROQ_API_KEY": "g1", "GEMINI_API_KEY": "a1", "GEMINI_API_KEY_2": "a2"}
ASK = [Message(role="user", content="What is the forecast for Pune?")]

#: Constructed the same way `test_client_quota.py` constructs it, and for the same reason:
#: Groq has only ever been observed refusing on RPM, and a run cannot reach the end of its
#: pools until every provider is shut. The Google half — the half that carries the quota id
#: this test is about — is verbatim.
GROQ_RPD_429 = (
    '{"error":{"message":"Rate limit reached for model `openai/gpt-oss-20b` in organization '
    "`org_01knx5npqkfc19q5jefzfvdfv5` service tier `on_demand` on requests per day (RPD): "
    'Limit 1000, Used 1000, Requested 1."}}'
)

DAILY_QUOTA_ID = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"


def rows_of(path: Path, kind: str) -> list[dict]:
    return [row for row in read_rows(path) if row["kind"] == kind]


async def test_a_daily_wall_ends_a_run_and_the_quota_id_survives_to_the_ledger(tmp_path):
    """Recorded body → classifier → every pool shut → run incomplete → wall row on disk.

    The five steps are each tested on their own elsewhere. What is only testable here is
    that they are the same five steps: that the ``AllPoolsExhausted`` the scheduler raises
    is the one the budget guard reads as ``pools_exhausted``, and that the refusal which
    shut the last pool lands in the run's own ledger with ``quotaId`` and ``quotaValue``
    intact.

    That last part is not bookkeeping. Google publishes no per-model daily figure for the
    free tier at all, so this project's only route to that number is a 429 body kept whole,
    in order beside the attempts around it. A run that ends against this wall and does not
    record it has to be run again to learn what it already touched.
    """
    clock = FakeClock(wall=datetime(2026, 9, 6, 18, 0, tzinfo=UTC))  # 11:00 Pacific
    http = FakeHttp()
    http.replies = [
        response(GROQ_RPD_429, status=429),
        response(GOOGLE_DAILY_429, status=429),
        response(GOOGLE_DAILY_429, status=429),
    ]

    ledger = RunLedger("r-exhausted", root=tmp_path / "runs", clock=clock)
    client = Client(
        Registry(ClientConfig.load(), http, ALL_KEYS),
        clock=clock,
        rng=random.Random(20260906),
        quota_walls=ledger.on_quota_wall,
    )
    config = RunConfig.start(
        "A0",
        [f"dev-{i:04d}" for i in range(6)],
        run_id="r-exhausted",
        token_ceiling=1_000_000,
        wall_clock_ceiling_s=3_600.0,
        concurrency=1,
    )

    asked: list[str] = []

    async def executor(context):
        asked.append(context.task_id)
        context.record(await client.complete("cheap", ASK))

    with ledger:
        report = await Run(config, ledger, clock=clock).execute(executor)

    # -- the run stopped, and said which of the six reasons it was ------------------------
    assert report.status == STATUS_INCOMPLETE
    assert report.incomplete_reason == IncompleteReason.POOLS_EXHAUSTED
    # One task proved the wall. The remaining five would have proved it five more times
    # against a ceiling that does not move until tomorrow.
    assert asked == ["dev-0000"]
    assert report.tasks_complete == 0
    assert report.tasks_failed == 1
    assert report.tasks_remaining == 6

    # -- every pool was tried once, and none was retried or slept on ----------------------
    assert [r.pool for r in http.requests] == ["groq#1", "google-ai-studio#1", "google-ai-studio#2"]
    assert clock.slept == []

    # -- the refusals reached the run's own ledger, in order, whole -----------------------
    walls = rows_of(ledger.path, "wall")
    assert [w["pool"] for w in walls] == ["groq#1", "google-ai-studio#1", "google-ai-studio#2"]
    assert [w["run_id"] for w in walls] == ["r-exhausted"] * 3

    google = walls[1]
    assert google["status"] == 429
    assert google["scope"] == "day"
    assert google["reason"] == "quota_day"
    assert google["quota_id"] == DAILY_QUOTA_ID
    assert google["quota_value"] == 20
    assert google["quota_metric"].endswith("generate_content_free_tier_requests")
    # The hint the body gives beside the quota, kept but not obeyed: honouring 32 seconds
    # against a ceiling that clears at midnight Pacific earns another 429 at 32 seconds.
    assert google["retry_after_s"] == pytest.approx(32.0)
    assert google["blocked_for_s"] == pytest.approx(13 * 3600, abs=1.0)
    assert google["body"] == GOOGLE_DAILY_429

    # -- and the ordering that makes it reconstructable -----------------------------------
    # Which wall shut which pool during which task is the question Phase 6 asks, and it is
    # only answerable because these share one file with the attempts around them.
    kinds = [row["kind"] for row in read_rows(ledger.path)]
    assert kinds == ["run_start", "wall", "wall", "wall", "task", "run_end"]

    end = rows_of(ledger.path, "run_end")[0]
    assert end["incomplete_reason"] == IncompleteReason.POOLS_EXHAUSTED


async def test_the_exception_that_ends_the_run_names_when_the_pools_come_back(tmp_path):
    """A run that stops has to tell whoever resumes it whether to wait or to go away.

    ``AllPoolsExhausted`` is the client's only exception carrying a time, and this is the
    seam where that time stops being a detail of quota control and becomes the thing an
    operator acts on. It is asserted here rather than in the client's own suite because
    what matters is that it is still intact after crossing.
    """
    clock = FakeClock(wall=datetime(2026, 9, 6, 18, 0, tzinfo=UTC))
    http = FakeHttp()
    http.replies = [response(GOOGLE_DAILY_429, status=429)] * 3
    http.replies[0] = response(GROQ_RPD_429, status=429)

    client = Client(
        Registry(ClientConfig.load(), http, ALL_KEYS),
        clock=clock,
        rng=random.Random(20260906),
    )
    raised: list[AllPoolsExhausted] = []

    async def executor(context):
        try:
            context.record(await client.complete("cheap", ASK))
        except AllPoolsExhausted as exc:
            raised.append(exc)
            raise

    ledger = RunLedger("r-when", root=tmp_path / "runs", clock=clock)
    config = RunConfig.start(
        "A0",
        ["dev-0000"],
        run_id="r-when",
        token_ceiling=1_000_000,
        wall_clock_ceiling_s=3_600.0,
        concurrency=1,
    )
    with ledger:
        report = await Run(config, ledger, clock=clock).execute(executor)

    assert report.incomplete_reason == IncompleteReason.POOLS_EXHAUSTED
    assert len(raised) == 1
    exhausted = raised[0]
    assert exhausted.role == "cheap"
    assert exhausted.ready_in_s > 0
    assert exhausted.ready_at_utc > clock.now_utc()
    # Every wall that shut a pool travels with it, so the decision to wait or to stop is
    # made against what actually refused rather than against a count.
    assert {w.pool for w in exhausted.walls} == {
        "groq#1",
        "google-ai-studio#1",
        "google-ai-studio#2",
    }
    assert any(w.quota_id == DAILY_QUOTA_ID for w in exhausted.walls)
