"""Quota control: which wall was hit, what it costs, and where the work goes instead.

Three things this suite is careful about.

**No wall-clock time passes.** Every wait goes through a stub clock that records the delay
and advances virtual time, so the backoff *schedule* is asserted — the delays doubled, they
sat inside their jitter band, a daily wall shut a pool for hours — without any of it being
spent. A test that slept through its own backoff would be slow, and a slow timing test is a
flaky one.

**No live call, and no key.** Every provider here is a stub answering from a script.

**The refusals are the ones providers actually sent**, lifted from the 0.4 inventory.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, datetime

import pytest

from conftest import (
    CLOUDFLARE_1010_403,
    GOOGLE_DAILY_429,
    GOOGLE_MINUTE_429,
    GOOGLE_RETIRED_404,
    GROQ_RATE_LIMIT_HEADERS,
    GROQ_RPM_429,
    FakeClock,
    FakeHttp,
    google_reply,
    groq_reply,
    response,
)
from query_pilot.client.buckets import ModelBuckets, TokenBucket
from query_pilot.client.classify import Action, Scope, classify
from query_pilot.client.client import Client
from query_pilot.client.config import ClientConfig, Limits
from query_pilot.client.errors import (
    MalformedResponseError,
    ProviderHTTPError,
    QuotaFact,
    TransportError,
)
from query_pilot.client.registry import Registry
from query_pilot.client.resets import UNKNOWN_DAILY_REPROBE_S, seconds_until_daily_reset
from query_pilot.client.retry import RetryPolicy
from query_pilot.client.scheduler import AllPoolsExhausted, JsonlQuotaWalls, QuotaWall
from query_pilot.client.types import Message

ALL_KEYS = {"GROQ_API_KEY": "g1", "GEMINI_API_KEY": "a1", "GEMINI_API_KEY_2": "a2"}
ASK = [Message(role="user", content="What is the forecast for Pune?")]

#: Constructed, not recorded: Groq has only ever been observed refusing on RPM. This is
#: that body with the dimension it names changed to the daily one, which is the shape its
#: documented 200,000-tokens-a-day ceiling would arrive in.
GROQ_RPD_429 = GROQ_RPM_429.replace(
    "on requests per minute (RPM): Limit 30", "on requests per day (RPD): Limit 1000"
)

GROQ_CHEAP = ("groq", "groq#1", "openai/gpt-oss-20b")
GOOGLE_1 = ("google-ai-studio", "google-ai-studio#1", "gemini-3.5-flash-lite")
GOOGLE_2 = ("google-ai-studio", "google-ai-studio#2", "gemini-3.5-flash-lite")


def client_for(http: FakeHttp, clock: FakeClock, **kwargs) -> Client:
    """A client on the committed configuration, with every key present and no real time."""
    config = ClientConfig.load()
    return Client(
        Registry(config, http, ALL_KEYS),
        clock=clock,
        rng=random.Random(20260906),
        **kwargs,
    )


def error(
    status: int, body: str, *, provider: str = "google-ai-studio", **quota
) -> ProviderHTTPError:
    return ProviderHTTPError(
        status=status,
        body=body,
        provider=provider,
        model="gemini-3.5-flash-lite",
        pool=f"{provider}#1",
        quota=QuotaFact(**quota) if quota else None,
    )


# --- classification -------------------------------------------------------------------


def test_a_daily_wall_and_a_minute_wall_are_not_the_same_condition():
    minute = classify(
        error(429, GOOGLE_MINUTE_429, quota_id="X-PerMinutePerProject", retry_after_s=26.0)
    )
    day = classify(error(429, GOOGLE_DAILY_429, quota_id="X-PerDayPerProject", retry_after_s=32.0))

    assert (minute.action, minute.scope) == (Action.BLOCK, Scope.MINUTE)
    assert minute.block_for_s == pytest.approx(26.0)
    assert (day.action, day.scope) == (Action.BLOCK, Scope.DAY)
    # How long a daily wall lasts is a question about the calendar, answered where the
    # calendar is known rather than by whatever the provider suggested.
    assert day.block_for_s is None


def test_the_quota_name_outranks_the_providers_own_retry_hint():
    """The one place a provider's explicit instruction is deliberately disregarded.

    The recorded daily refusal carries `retryDelay: 32s` beside a quotaId naming a *per
    day* ceiling. Honouring the hint earns another 429 thirty-two seconds later, and again,
    until the day ends.
    """
    body = json.loads(GOOGLE_DAILY_429)
    hints = [d for d in body["error"]["details"] if d["@type"].endswith("RetryInfo")]
    assert hints == [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "32s"}]

    decision = classify(
        error(
            429,
            GOOGLE_DAILY_429,
            quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            retry_after_s=32.0,
        )
    )
    assert decision.scope is Scope.DAY
    assert decision.block_for_s is None


def test_groq_names_its_ceiling_in_prose_and_that_is_read_too():
    decision = classify(
        error(429, GROQ_RPM_429, provider="groq", quota_id="RPM", retry_after_s=2.0)
    )
    assert (decision.action, decision.scope) == (Action.BLOCK, Scope.MINUTE)
    assert decision.block_for_s == pytest.approx(2.0)


def test_a_429_that_names_nothing_is_treated_as_the_shorter_wall():
    # Conservative in the direction that costs least: assume it clears within the minute,
    # find out otherwise by asking again.
    decision = classify(error(429, "too many requests"))
    assert decision.action is Action.BLOCK
    assert decision.scope is Scope.UNKNOWN
    assert decision.block_for_s == pytest.approx(60.0)


def test_a_bot_block_is_terminal_but_is_not_an_auth_failure():
    """The 403 that is neither cleanly retryable nor cleanly terminal.

    Cloudflare answers Python's default User-Agent with this, and the 0.4 inventory
    reported every Groq model dead because of it. Retrying an identical request cannot
    succeed, so it does not retry — but calling it an auth failure sends someone to rotate
    a key that was never the problem.
    """
    decision = classify(error(403, CLOUDFLARE_1010_403, provider="groq"))
    assert decision.action is Action.TERMINAL
    assert decision.reason == "bot_block"
    assert decision.reason != "auth"


def test_a_403_that_really_is_auth_says_so():
    decision = classify(error(403, '{"error":{"message":"Invalid API key"}}'))
    assert (decision.action, decision.reason) == (Action.TERMINAL, "auth")


def test_a_403_nobody_recognises_is_named_rather_than_absorbed():
    # It must reach the ledger as its own thing. Folding an unfamiliar 403 into "auth" is
    # how the Cloudflare block went unexplained for a whole run in the first place.
    decision = classify(error(403, "denied by policy 47"))
    assert (decision.action, decision.reason) == (Action.TERMINAL, "forbidden_unclassified")


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (404, GOOGLE_RETIRED_404, "model_not_found"),
        (401, '{"error":"unauthorized"}', "auth"),
        (400, '{"error":"malformed request"}', "bad_request"),
        (402, '{"error":"Payment required to access this resource."}', "payment_required"),
    ],
)
def test_terminal_statuses_are_named(status, body, reason):
    decision = classify(error(status, body))
    assert decision.action is Action.TERMINAL
    assert decision.reason == reason


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408])
def test_server_errors_and_timeouts_are_retryable(status):
    # 503 is here on evidence: gemini-3.8-flash returned one mid-probe at 0.4 and answered
    # on the next call.
    assert classify(error(status, "upstream unavailable")).action is Action.RETRY


def test_a_transport_failure_is_retryable_and_an_unreadable_answer_is_not():
    transport = TransportError("timeout", provider="groq", model="m", pool="groq#1")
    unreadable = MalformedResponseError("no candidates", provider="groq", model="m", pool="groq#1")
    assert classify(transport).action is Action.RETRY
    assert classify(unreadable).action is Action.TERMINAL


# --- the backoff schedule, asserted rather than endured -------------------------------


def test_the_nominal_schedule_doubles_and_is_capped():
    policy = RetryPolicy(base_s=0.5, factor=2.0, max_delay_s=4.0)
    assert [policy.nominal_s(n) for n in range(1, 6)] == [0.5, 1.0, 2.0, 4.0, 4.0]


def test_every_delay_sits_inside_its_jitter_band():
    policy = RetryPolicy(jitter=0.25)
    rng = random.Random(20260906)
    for attempt in range(1, 5):
        nominal = policy.nominal_s(attempt)
        for _ in range(200):
            delay = policy.delay_s(attempt, rng)
            assert 0.75 * nominal <= delay <= 1.25 * nominal
    # Jitter is not decoration: several coroutines refused in the same instant must not
    # come back in the same instant.
    assert len({policy.delay_s(1, rng) for _ in range(50)}) > 1


async def test_a_retryable_failure_is_backed_off_on_the_schedule(http, clock):
    policy = RetryPolicy(max_attempts=4, base_s=0.5, factor=2.0, jitter=0.25)
    http.replies = [
        response("upstream unavailable", status=503),
        response("upstream unavailable", status=503),
        groq_reply(),
    ]
    completion = await client_for(http, clock, policy=policy).complete("cheap", ASK)

    assert completion.text == "ready"
    assert len(clock.slept) == 2
    for attempt, waited in enumerate(clock.slept, start=1):
        nominal = policy.nominal_s(attempt)
        assert 0.75 * nominal <= waited <= 1.25 * nominal
    assert clock.slept[1] > clock.slept[0]


async def test_a_terminal_error_is_not_retried(http, clock):
    http.replies = [response(GOOGLE_RETIRED_404, status=404)]
    with pytest.raises(ProviderHTTPError) as raised:
        await client_for(http, clock).complete("cheap", ASK)
    assert raised.value.status == 404
    assert len(http.requests) == 1
    assert clock.slept == []


async def test_a_terminal_error_does_not_spill_to_another_provider(http, clock):
    # Quietly serving a run from a different model than the one it declared corrupts a
    # measurement rather than rescuing it.
    http.replies = [response(CLOUDFLARE_1010_403, status=403)]
    with pytest.raises(ProviderHTTPError):
        await client_for(http, clock).complete("cheap", ASK)
    assert [r.provider for r in http.requests] == ["groq"]


async def test_a_pool_that_keeps_failing_is_set_aside_and_the_work_moves(http, clock):
    policy = RetryPolicy(max_attempts=3)
    http.replies = [response("boom", status=500)] * 3 + [google_reply()]
    completion = await client_for(http, clock, policy=policy).complete("cheap", ASK)

    assert [r.provider for r in http.requests] == ["groq"] * 3 + ["google-ai-studio"]
    assert completion.provider == "google-ai-studio"


# --- buckets ---------------------------------------------------------------------------


def test_a_bucket_empties_and_refills_continuously(clock):
    bucket = TokenBucket(capacity=30, refill_per_s=30 / 60, clock=clock)
    for _ in range(30):
        bucket.take()
    assert bucket.available == pytest.approx(0.0)
    # Groq's requests-per-day countdown was observed advancing one thousandth of a day per
    # request, which is a bucket refilling rather than resetting on a boundary.
    clock.advance(10)
    assert bucket.available == pytest.approx(5.0)
    assert bucket.ready_at(1.0) <= clock.monotonic()


def test_an_unmodelled_dimension_does_not_hold_anything_back(clock):
    # Google publishes no per-model daily figure and none has been observed. Absent means
    # unmodelled: the client cannot pre-empt that wall, only walk into it and record it.
    buckets = ModelBuckets(limits=Limits(rpm=15), clock=clock)
    assert buckets.requests_per_day is None and buckets.tokens_per_day is None
    for _ in range(15):
        buckets.charge_request()
    assert not buckets.ready()
    clock.advance(60)
    assert buckets.ready()


def test_tokens_are_charged_after_the_answer_and_can_run_a_bucket_into_deficit(clock):
    buckets = ModelBuckets(limits=Limits(tpm=8000), clock=clock)
    buckets.charge_tokens(9000)
    assert buckets.tokens_per_minute.available == pytest.approx(-1000.0)
    assert not buckets.ready()
    clock.advance(5)  # refills at 8000/60 a second, so the deficit clears in 7.5
    assert not buckets.ready()
    clock.advance(5)
    assert buckets.ready()


def test_a_providers_own_count_lowers_a_bucket_but_never_raises_it(clock):
    buckets = ModelBuckets(limits=Limits(rpd=1000, tpm=8000), clock=clock)
    # A previous run spent quota this process never saw.
    buckets.observe(requests_remaining=926, tokens_remaining=6130)
    assert buckets.requests_per_day.available == pytest.approx(926)
    assert buckets.tokens_per_minute.available == pytest.approx(6130)
    # More room than the client modelled is not a reason to spend past a modelled ceiling.
    buckets.observe(requests_remaining=999_999, tokens_remaining=999_999)
    assert buckets.requests_per_day.available == pytest.approx(926)


async def test_a_successful_call_charges_the_tokens_it_reported(http, clock):
    http.replies = [groq_reply(prompt_tokens=78, completion_tokens=42)]
    client = client_for(http, clock)
    await client.complete("cheap", ASK)
    buckets = client.scheduler.book.known()[GROQ_CHEAP]
    assert buckets.tokens_per_minute.available == pytest.approx(8000 - 120)
    assert buckets.requests_per_minute.available == pytest.approx(29)


async def test_groqs_headers_correct_the_bucket_downwards(http, clock):
    http.replies = [groq_reply(headers=GROQ_RATE_LIMIT_HEADERS)]
    client = client_for(http, clock)
    await client.complete("cheap", ASK)
    buckets = client.scheduler.book.known()[GROQ_CHEAP]
    assert buckets.requests_per_day.available == pytest.approx(926)


# --- keying: per pool, per model -------------------------------------------------------


async def test_two_models_on_one_pool_do_not_share_a_ceiling(http, clock):
    """The roadmap said a bucket per provider. The measurements say otherwise.

    Groq serves requests-per-day and tokens-per-minute per model per organization — two of
    its models showed different remaining counters after identical bursts. A bucket keyed on
    the provider alone would block the strong tier because the cheap one spent its quota.
    """
    http.replies = [groq_reply(), groq_reply()]
    client = client_for(http, clock)
    await client.complete("cheap", ASK)
    await client.complete("strong", ASK)

    book = client.scheduler.book.known()
    strong = ("groq", "groq#1", "openai/gpt-oss-120b")
    assert book[GROQ_CHEAP] is not book[strong]
    # One request each, on one pool, one provider — and neither spent the other's quota.
    assert book[GROQ_CHEAP].requests_per_minute.available == pytest.approx(29)
    assert book[strong].requests_per_minute.available == pytest.approx(29)
    assert book[strong].requests_per_day.available == pytest.approx(999)


# --- spillover ---------------------------------------------------------------------------


async def test_a_minute_wall_moves_the_work_rather_than_waiting(http, clock):
    http.replies = [
        response(GROQ_RPM_429, status=429, headers={"retry-after": "2"}),
        google_reply(),
    ]
    completion = await client_for(http, clock).complete("cheap", ASK)

    assert completion.provider == "google-ai-studio"
    # Nothing was waited for. There was another pool with room.
    assert clock.slept == []
    assert [r.provider for r in http.requests] == ["groq", "google-ai-studio"]


async def test_work_walks_down_the_pools_of_one_provider_before_leaving_it(http, clock):
    http.replies = [
        response(GROQ_RPM_429, status=429, headers={"retry-after": "2"}),
        response(GOOGLE_MINUTE_429, status=429),
        google_reply(),
    ]
    client = client_for(http, clock)
    completion = await client.complete("cheap", ASK)

    assert [r.pool for r in http.requests] == [
        "groq#1",
        "google-ai-studio#1",
        "google-ai-studio#2",
    ]
    assert completion.pool == "google-ai-studio#2"

    # Two Google projects are two ceilings of 15 RPM, never one of 30. That is what the 0.4
    # independence probe established, and a shared bucket would throw away exactly the
    # capacity the second project was added for.
    book = client.scheduler.book.known()
    assert book[GOOGLE_1].blocked_reason == "quota_minute"
    assert book[GOOGLE_2].blocked_reason is None
    assert book[GOOGLE_2].requests_per_minute.available == pytest.approx(14)


async def test_a_daily_wall_shuts_a_pool_for_the_rest_of_the_day_not_for_its_retry_hint(
    http, clock
):
    """The failure a two-valued classifier walks straight into.

    The recorded body suggests retrying in 32 seconds. The quota it names does not move
    until midnight Pacific, and the pool is shut until then.
    """
    clock.wall = datetime(2026, 9, 6, 18, 0, tzinfo=UTC)  # 11:00 Pacific
    http.replies = [
        response(GROQ_RPM_429, status=429, headers={"retry-after": "2"}),
        response(GOOGLE_DAILY_429, status=429),
        google_reply(),
    ]
    client = client_for(http, clock)
    await client.complete("cheap", ASK)

    buckets = client.scheduler.book.known()[GOOGLE_1]
    blocked_for = buckets.blocked_until - 1_000.0
    assert buckets.blocked_reason == "quota_day"
    assert blocked_for == pytest.approx(13 * 3600, abs=1.0)
    assert blocked_for > 32.0 * 100


async def test_when_every_pool_is_shut_for_the_day_the_call_raises_rather_than_blocks(http, clock):
    """Blocking forever is wrong and spinning is worse, so it says so and stops.

    The budget guard is what turns this into a decision about the run. What this owes it is
    an exception that names when the earliest pool returns.
    """
    clock.wall = datetime(2026, 9, 6, 18, 0, tzinfo=UTC)
    http.replies = [
        response(GROQ_RPD_429, status=429),
        response(GOOGLE_DAILY_429, status=429),
        response(GOOGLE_DAILY_429, status=429),
    ]
    client = client_for(http, clock)
    with pytest.raises(AllPoolsExhausted) as raised:
        await client.complete("cheap", ASK)

    # Every candidate was tried once, and none was retried against a wall that cannot lift.
    assert [r.pool for r in http.requests] == ["groq#1", "google-ai-studio#1", "google-ai-studio#2"]
    assert clock.slept == []
    # The soonest anything reopens is Groq's re-probe in an hour, far past the ceiling.
    assert raised.value.ready_in_s == pytest.approx(UNKNOWN_DAILY_REPROBE_S)
    assert raised.value.ready_in_s > ClientConfig.load().settings.wait_ceiling_s
    assert raised.value.ready_at_utc > clock.now_utc()
    assert any(wall.scope == "day" for wall in raised.value.walls)
    assert "quota_day" in str(raised.value)


# --- what a wall leaves behind -------------------------------------------------------------


async def test_a_daily_wall_records_everything_needed_to_fill_in_the_number(http, clock):
    """Google's daily allowance is the project's most valuable unmeasured number.

    The first run to hit that ceiling should be the run that measures it, not the one that
    discovers a second run is needed. That means status, the whole body, the quota's own id
    and value, and when it happened.
    """
    walls: list[QuotaWall] = []
    http.replies = [
        response(GROQ_RPM_429, status=429, headers={"retry-after": "2"}),
        response(GOOGLE_DAILY_429, status=429),
        google_reply(),
    ]
    client = client_for(http, clock, quota_walls=walls.append)
    await client.complete("cheap", ASK)

    # Both refusals are recorded, each with the ceiling its own provider named.
    assert [w.reason for w in walls] == ["quota_minute", "quota_day"]
    wall = walls[1]
    assert wall.status == 429
    assert wall.scope == "day"
    assert wall.reason == "quota_day"
    assert wall.quota_id == "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    assert wall.quota_value == 20
    assert wall.quota_metric.endswith("generate_content_free_tier_requests")
    assert wall.retry_after_s == pytest.approx(32.0)
    assert wall.pool == "google-ai-studio#1"
    assert wall.model == "gemini-3.5-flash-lite"
    assert wall.body == GOOGLE_DAILY_429  # whole, not summarised
    assert datetime.fromisoformat(wall.observed_at).tzinfo is not None


async def test_walls_are_appended_as_json_lines(http, clock, tmp_path):
    path = tmp_path / "runs" / "quota-walls.jsonl"
    http.replies = [
        response(GROQ_RPM_429, status=429, headers={"retry-after": "2"}),
        response(GOOGLE_MINUTE_429, status=429),
        google_reply(),
    ]
    client = client_for(http, clock, quota_walls=JsonlQuotaWalls(path))
    await client.complete("cheap", ASK)

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["provider"] for row in rows] == ["groq", "google-ai-studio"]
    assert rows[1]["quota_id"] == "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    assert rows[1]["quota_value"] == 15
    assert rows[1]["blocked_for_s"] == pytest.approx(26.0)
    # Whole, so a ceiling a provider names once never has to be named twice.
    assert json.loads(rows[1]["body"])["error"]["code"] == 429


# --- concurrency ----------------------------------------------------------------------------


async def test_concurrency_is_bounded_per_provider(clock):
    """What the caps really bound is token overshoot.

    Tokens are charged after a response, so a ceiling can be exceeded by whatever is in
    flight. At Groq's observed 30 RPM and 0.5-0.9 s latency the rate limit permits about a
    third of a request in flight, so the semaphore is never what binds; it is what keeps
    the overshoot to a few percent of an 8,000-token minute.
    """

    class GatedHttp:
        def __init__(self) -> None:
            self.live = 0
            self.peak = 0
            self.gate = asyncio.Event()

        async def post(self, url, *, headers, json, provider, model, pool):
            self.live += 1
            self.peak = max(self.peak, self.live)
            await self.gate.wait()
            self.live -= 1
            return groq_reply()

        async def aclose(self) -> None:
            pass

    gated = GatedHttp()
    config = ClientConfig.load()
    client = Client(Registry(config, gated, ALL_KEYS), clock=clock)
    calls = [asyncio.create_task(client.complete("cheap", ASK)) for _ in range(10)]
    await asyncio.sleep(0)
    gated.gate.set()
    await asyncio.gather(*calls)

    assert gated.peak <= config.settings.per_provider_concurrency
    assert gated.peak <= config.settings.max_concurrency


# --- daily reset boundaries --------------------------------------------------------------------


def test_google_resets_at_midnight_pacific():
    spec = ClientConfig.load().providers["google-ai-studio"]
    # 18:00 UTC on 6 September is 11:00 Pacific, and Pacific is on daylight time then.
    seconds = seconds_until_daily_reset(spec, datetime(2026, 9, 6, 18, 0, tzinfo=UTC))
    assert seconds == pytest.approx(13 * 3600)


def test_groq_has_no_reset_boundary_because_it_refills_continuously():
    spec = ClientConfig.load().providers["groq"]
    assert seconds_until_daily_reset(spec, datetime(2026, 9, 6, 18, 0, tzinfo=UTC)) is None


async def test_an_unknown_boundary_becomes_a_re_probe_rather_than_a_guess(http, clock):
    """A daily wall on a provider with no configured boundary.

    Writing the pool off for a day would throw away capacity that may have returned within
    the hour; guessing a boundary would invent a measurement. So it waits an hour and asks
    again.
    """
    http.replies = [response(GROQ_RPD_429, status=429), google_reply()]
    client = client_for(http, clock)
    await client.complete("cheap", ASK)

    # Groq has no boundary configured, because its daily ceiling was observed refilling
    # continuously rather than resetting at an hour.
    blocked = client.scheduler.book.known()[GROQ_CHEAP]
    assert blocked.blocked_reason == "quota_day"
    assert blocked.blocked_until - 1_000.0 == pytest.approx(UNKNOWN_DAILY_REPROBE_S)
