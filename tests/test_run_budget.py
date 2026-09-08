"""The budget guard: stopping a run at a declared ceiling, and refusing to dress the
result up as a finished one.

**No test here makes a live API call and none needs a key.** Every task is a stub that
spends a stated number of tokens, and no ceiling is ever waited through: the clock is an
argument, so a four-hour wall clock is tested in microseconds.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import GOOGLE_DAILY_429
from query_pilot.agents import ROLE
from query_pilot.client.config import ClientConfig
from query_pilot.client.errors import ConfigError, ProviderHTTPError, TransportError
from query_pilot.client.scheduler import AllPoolsExhausted
from query_pilot.client.types import Completion
from query_pilot.run import (
    IncompleteReason,
    IncompleteRun,
    Run,
    RunConfig,
    RunConfigChanged,
    RunLedger,
    TaskResult,
    read_rows,
    read_summary,
    summarise,
    write_summary,
)

REPO = Path(__file__).resolve().parents[1]
WORKING_SET_CONFIG = REPO / "config" / "runs" / "working-set.toml"


class SteppingClock:
    """Virtual time that only moves when a test says so."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def now_utc(self) -> datetime:
        return datetime(2026, 9, 8, tzinfo=UTC) + timedelta(seconds=self.t)

    async def sleep(self, seconds: float) -> None:
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


def completion(*, prompt_tokens: int = 80, completion_tokens: int = 20) -> Completion:
    return Completion(
        text="ready",
        tool_calls=(),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        provider="groq",
        model="openai/gpt-oss-20b",
        pool="groq#1",
        latency_s=0.61,
        finish_reason="stop",
    )


def config_for(task_ids, *, run_id="r-1", token_ceiling=10_000_000, wall=86_400.0, concurrency=1):
    return RunConfig.start(
        "A0",
        list(task_ids),
        run_id=run_id,
        token_ceiling=token_ceiling,
        wall_clock_ceiling_s=wall,
        concurrency=concurrency,
    )


def ledger_for(tmp_path, run_id="r-1", clock=None) -> RunLedger:
    return RunLedger(run_id, root=tmp_path / "runs", clock=clock or SteppingClock())


def rows_of(path: Path, kind: str) -> list[dict]:
    return [row for row in read_rows(path) if row["kind"] == kind]


class Spender:
    """Spends a fixed number of tokens per task and records who was asked."""

    def __init__(self, *, cost: int = 100, clock: SteppingClock | None = None, step: float = 0.0):
        self.calls: list[str] = []
        self.cost = cost
        self.clock = clock
        self.step = step

    async def __call__(self, context):
        self.calls.append(context.task_id)
        await asyncio.sleep(0)
        if self.clock is not None:
            self.clock.advance(self.step)
        context.record(completion(prompt_tokens=self.cost, completion_tokens=0))
        return TaskResult()


# --- the ceilings, and where their numbers come from -----------------------------------------


def test_the_committed_run_config_declares_ceilings_that_trace_to_measured_capacity():
    """The one place this project writes an unmeasured number, so the arithmetic is tested."""
    config = RunConfig.load(WORKING_SET_CONFIG, [f"t-{i}" for i in range(150)], run_id="r-1")

    # docs/PROVIDERS.md's cliff table, the 10,000-tokens-a-task row: 150 tasks, one run.
    assert config.token_ceiling == 150 * 10_000 == 1_500_000
    # And the same figure as 2.5 Groq-days against the 600,000 tokens a day that document
    # records as this project's stated capacity (3 usable models x a published 200,000).
    assert config.token_ceiling == 2.5 * (3 * 200_000)

    # 150 tasks at Groq's observed 30 RPM is a floor of 300 seconds of request time. The
    # ceiling sits far above that floor deliberately: it stops a stuck run, not a slow one.
    assert config.wall_clock_ceiling_s == 14_400
    assert config.wall_clock_ceiling_s > 150 / 30 * 60

    assert config.concurrency == 1
    assert config.agent == "A0" and config.params == {"split": "working"}


def test_the_declared_concurrency_cannot_burst_past_the_endpoints_per_minute_token_ceiling():
    """What the concurrency of 1 was derived from, and what it admits and rejects.

    Tokens are charged to the buckets when an answer arrives, so `concurrency` requests are
    in flight before any of them is paid for. Charged together they put the per-minute token
    bucket into a deficit of about `concurrency x tokens-per-attempt`, which refills at
    `tpm / 60` a second. A deficit deeper than `wait_ceiling_s` of refill makes the client
    raise `AllPoolsExhausted`, which `fatal_reason` treats as fatal — so the run ends as
    `pools_exhausted` **without a provider having refused anything.** Simulated over the
    real 150 tasks, concurrency 8 ended the run at task 91 of 150.

    Every number here is read from a committed file rather than written in this test:
    `docs/a0-prompt-sizes.json` for what an attempt costs and `config/providers.toml` for
    what the endpoint allows. No substrate and no keys are needed to check the arithmetic.
    """
    sizes = json.loads((REPO / "docs" / "a0-prompt-sizes.json").read_text())
    client_config = ClientConfig.load()
    limits = client_config.endpoints[client_config.roles[ROLE].endpoint].limits
    config = RunConfig.load(WORKING_SET_CONFIG, [f"t-{i}" for i in range(150)], run_id="r-1")

    worst_case = sizes["worst_case_attempt_tokens"]
    assert worst_case == sizes["prompt_tokens_estimated"]["max"] + sizes["max_output_tokens"]
    assert sizes["split"] == config.params["split"]

    # A deficit is survivable while it can be repaid inside the wait ceiling.
    survivable = limits.tpm / 60.0 * client_config.settings.wait_ceiling_s
    assert config.concurrency * worst_case <= survivable

    # And it admits by a margin rather than by a hair. What it rejects, at these sizes:
    assert 4 * worst_case > survivable
    assert 8 * worst_case > survivable
    assert config.concurrency * worst_case * 3 < survivable

    # The median attempt would survive eight in flight, which is why this is not visible
    # in an average and was not visible in 2.3's ten smoke tasks. What breaks it is that
    # the split's order groups by database — up to `longest_same_database_run` tasks in a
    # row share one schema — so a burst is usually a burst of one prompt size, and on the
    # largest schema in the split that size alone is enough.
    assert sizes["longest_same_database_run"] >= 8
    largest = sizes["largest_database"]["prompt_tokens_estimated"]
    assert 8 * largest > survivable
    assert 8 * (sizes["prompt_tokens_estimated"]["median"] + 200) < survivable


def test_a_run_config_file_missing_a_ceiling_is_refused(tmp_path):
    bad = tmp_path / "run.toml"
    bad.write_text('[run]\nagent = "A0"\ntoken_ceiling = 1000\n')
    with pytest.raises(Exception, match="missing 'wall_clock_ceiling_s'"):
        RunConfig.load(bad, ["t-1"])


# --- the token ceiling -------------------------------------------------------------------------


async def test_a_run_stops_at_its_token_ceiling_and_says_so_rather_than_truncating(tmp_path):
    ledger = ledger_for(tmp_path)
    config = config_for([f"t-{i}" for i in range(20)], token_ceiling=250)
    spender = Spender(cost=100)

    with ledger:
        report = await Run(config, ledger).execute(spender)

    assert report.status == "incomplete"
    assert report.incomplete_reason == IncompleteReason.TOKEN_CEILING
    assert len(spender.calls) == 3  # 300 tokens: the third crossed 250
    assert report.tasks_remaining == 17  # not skipped quietly; named as remaining
    end = rows_of(ledger.path, "run_end")[-1]
    assert end["status"] == "incomplete" and end["incomplete_reason"] == "token_ceiling"


async def test_a_refused_attempt_costs_nothing_and_a_failed_one_that_reported_tokens_counts(
    tmp_path,
):
    """The guard enforces spend. Phase 5's cost per solved task pays for failures too."""
    ledger = ledger_for(tmp_path)
    config = config_for([f"t-{i}" for i in range(20)], token_ceiling=250)
    calls: list[str] = []

    async def executor(context):
        calls.append(context.task_id)
        # A quota refusal: the provider reported no tokens, so it must cost nothing.
        context.record_failure(
            ProviderHTTPError(
                status=429,
                body=GOOGLE_DAILY_429,
                provider="google-ai-studio",
                model="gemini-3.8-flash",
                pool="google-ai-studio#1",
            )
        )
        # A generation that answered and was wrong still burned real tokens.
        context.record(completion(prompt_tokens=100, completion_tokens=0), turn=2)
        return TaskResult()

    with ledger:
        report = await Run(config, ledger).execute(executor)

    assert report.total_tokens == 300 and len(calls) == 3
    assert report.incomplete_reason == IncompleteReason.TOKEN_CEILING
    # Six attempts, three of which cost nothing: the refusals are recorded, not charged.
    assert report.attempts == 6


async def test_the_token_ceiling_is_inherited_by_a_resumed_run(tmp_path):
    """The single most consequential decision in the item, and it has a test.

    Refreshing per session would let a run spanning three days spend three times what it
    declared, which is the ceiling quietly ceasing to be one.
    """
    ledger = ledger_for(tmp_path)
    config = config_for([f"t-{i}" for i in range(20)], token_ceiling=250)

    with ledger:
        first = await Run(config, ledger).execute(Spender(cost=100))
    assert first.total_tokens == 300

    resumed = Spender(cost=100)
    with ledger:
        second = await Run(config, ledger).execute(resumed)

    assert resumed.calls == []  # nothing left to spend
    assert second.incomplete_reason == IncompleteReason.TOKEN_CEILING
    assert second.total_tokens == 300  # cumulative, not restarted
    assert second.tasks_complete == 3


async def test_the_wall_clock_ceiling_is_not_inherited_because_a_run_spans_a_quota_reset(
    tmp_path,
):
    """Tokens are a stock; time is a rate. A run waiting for midnight Pacific is normal."""
    clock = SteppingClock()
    ledger = ledger_for(tmp_path, clock=clock)
    config = config_for([f"t-{i}" for i in range(9)], wall=10.0)

    first = Spender(cost=1, clock=clock, step=4.0)
    with ledger:
        one = await Run(config, ledger, clock=clock).execute(first)
    assert one.incomplete_reason == IncompleteReason.WALL_CLOCK_CEILING
    assert first.calls == ["t-0", "t-1", "t-2"]  # 12s elapsed crossed a 10s ceiling

    second = Spender(cost=1, clock=clock, step=4.0)
    with ledger:
        two = await Run(config, ledger, clock=clock).execute(second)

    # A fresh session clock, so the run continues rather than being aborted for waiting.
    assert second.calls == ["t-3", "t-4", "t-5"]
    assert two.incomplete_reason == IncompleteReason.WALL_CLOCK_CEILING
    assert two.tasks_complete == 6


# --- the other ways a run does not finish -------------------------------------------------------


async def test_every_pool_shut_for_the_day_ends_the_run_as_its_own_reason(tmp_path):
    """1.2 raises this. 1.4 is where it stops being an exception and becomes an outcome."""
    ledger = ledger_for(tmp_path)
    config = config_for([f"t-{i}" for i in range(6)])
    calls: list[str] = []

    async def executor(context):
        calls.append(context.task_id)
        raise AllPoolsExhausted(
            "cheap",
            ready_in_s=46_800.0,
            ready_at_utc=datetime(2026, 9, 9, 7, 0, tzinfo=UTC),
            walls=[],
        )

    with ledger:
        report = await Run(config, ledger).execute(executor)

    assert report.incomplete_reason == IncompleteReason.POOLS_EXHAUSTED
    assert calls == ["t-0"]  # not five more proving the same wall
    assert report.tasks_failed == 1 and report.tasks_remaining == 6


@pytest.mark.parametrize(
    ("status", "reason"),
    [(404, "model_not_found"), (401, "auth"), (402, "payment_required")],
)
async def test_a_broken_configuration_stops_the_run_rather_than_burning_the_task_list(
    tmp_path, status, reason
):
    ledger = ledger_for(tmp_path)
    config = config_for([f"t-{i}" for i in range(6)])
    calls: list[str] = []

    async def executor(context):
        calls.append(context.task_id)
        raise ProviderHTTPError(
            status=status,
            body="no longer available",
            provider="google-ai-studio",
            model="gemini-2.5-flash",
            pool="google-ai-studio#1",
        )

    with ledger:
        report = await Run(config, ledger).execute(executor)

    assert report.incomplete_reason == IncompleteReason.CLIENT_FATAL
    assert calls == ["t-0"]
    (task,) = rows_of(ledger.path, "task")
    assert task["error_class"] == reason


async def test_a_missing_credential_stops_the_run_at_the_first_task(tmp_path):
    """The defect this closes, seen for real: ten tasks failed identically on one key.

    `ConfigError` is "the configuration or the environment cannot produce a working call",
    which is `client_fatal`'s definition written out. It classified as `unclassified`,
    which is not in `FATAL_ERROR_CLASSES`, so the run continued and burned the list proving
    the same thing ten times. At 150 tasks it would have proved it fifteen times over.
    """
    ledger = ledger_for(tmp_path)
    config = config_for([f"t-{i}" for i in range(6)])
    calls: list[str] = []

    async def executor(context):
        calls.append(context.task_id)
        raise ConfigError("role 'strong' has no credential pool; set one of: GROQ_API_KEY")

    with ledger:
        report = await Run(config, ledger).execute(executor)

    assert report.incomplete_reason == IncompleteReason.CLIENT_FATAL
    assert calls == ["t-0"]
    assert report.tasks_failed == 1 and report.tasks_remaining == 6
    (task,) = rows_of(ledger.path, "task")
    assert task["error_class"] == "config"
    # And the message survives to the row, which is the second half of the same defect.
    assert "GROQ_API_KEY" in task["detail"]["message"]


async def test_a_failure_that_is_about_one_task_does_not_stop_the_run(tmp_path):
    """`bad_request` and a timeout can be about this task's content. They are not fatal."""
    ledger = ledger_for(tmp_path)
    config = config_for([f"t-{i}" for i in range(4)])

    async def executor(context):
        if context.task_id == "t-1":
            raise ProviderHTTPError(
                status=400,
                body="messages: too long",
                provider="groq",
                model="openai/gpt-oss-20b",
                pool="groq#1",
            )
        if context.task_id == "t-2":
            raise TransportError(
                "read timeout", provider="groq", model="openai/gpt-oss-20b", pool="groq#1"
            )
        context.record(completion())
        return TaskResult()

    with ledger:
        report = await Run(config, ledger).execute(executor)

    assert report.status == "complete" and report.incomplete_reason is None
    assert report.tasks_complete == 2 and report.tasks_failed == 2
    classes = {r["task_id"]: r["error_class"] for r in rows_of(ledger.path, "task")}
    assert classes["t-1"] == "bad_request" and classes["t-2"] == "transport"


# --- draining, and how far a ceiling can be overshot ----------------------------------------------


async def test_in_flight_work_is_drained_rather_than_abandoned_and_the_overshoot_is_bounded(
    tmp_path,
):
    """Abandoning mid-write gives a half-row; waiting overshoots. Waiting, bounded, stated."""
    concurrency, cost, ceiling = 4, 100, 250
    ledger = ledger_for(tmp_path)
    config = config_for(
        [f"t-{i}" for i in range(40)], token_ceiling=ceiling, concurrency=concurrency
    )
    spender = Spender(cost=cost)

    with ledger:
        report = await Run(config, ledger).execute(spender)

    crossed_at = math.ceil(ceiling / cost)  # 3 tasks cross a 250 ceiling
    assert crossed_at <= len(spender.calls) <= crossed_at + concurrency - 1
    assert report.incomplete_reason == IncompleteReason.TOKEN_CEILING

    # Drained, not abandoned: every task that was dispatched has a terminal row, and every
    # attempt that was made is on disk.
    assert len(rows_of(ledger.path, "task")) == len(spender.calls)
    assert len(rows_of(ledger.path, "attempt")) == len(spender.calls)
    assert report.total_tokens == cost * len(spender.calls)


async def test_raising_a_ceiling_is_a_recorded_act_and_not_a_quiet_one(tmp_path):
    ledger = ledger_for(tmp_path)
    tasks = [f"t-{i}" for i in range(20)]
    tight = config_for(tasks, token_ceiling=250)
    with ledger:
        await Run(tight, ledger).execute(Spender(cost=100))

    roomy = config_for(tasks, token_ceiling=5_000)
    with ledger, pytest.raises(RunConfigChanged):
        await Run(roomy, ledger).execute(Spender(cost=100))

    with ledger:
        report = await Run(roomy, ledger).execute(Spender(cost=100), allow_config_change=True)

    start = rows_of(ledger.path, "run_start")[-1]
    assert start["previous_fingerprint"] == tight.fingerprint()
    assert start["declared"]["token_ceiling"] == 5_000
    assert report.status == "complete" and report.tasks_complete == 20


# --- the summary, which is the thing that gets copied out ------------------------------


async def incomplete_run(tmp_path) -> RunLedger:
    ledger = ledger_for(tmp_path)
    config = config_for([f"t-{i}" for i in range(20)], token_ceiling=250)
    with ledger:
        await Run(config, ledger).execute(Spender(cost=100))
    return ledger


async def test_a_summary_of_an_incomplete_run_contains_no_derived_number_at_all(tmp_path):
    """The label must survive being copied out, and the only way is for no number to exist."""
    ledger = await incomplete_run(tmp_path)
    summary = summarise(ledger.path)

    assert summary.complete is False
    assert summary.incomplete_reason == "token_ceiling"
    document = summary.as_file()
    assert next(iter(document)) == "warning"
    assert "INCOMPLETE RUN" in document["warning"]
    for name, value in document["derived"].items():
        assert value == "TBD (run incomplete: token_ceiling)", name
    # The counts are facts about what happened and do stay: hiding them makes an
    # interrupted run harder to resume rather than harder to misread.
    assert document["counts"]["tasks_complete"] == 3
    assert document["tokens"]["total"] == 300
    assert document["ceilings"]["token_ceiling"] == 250


async def test_the_summary_reader_refuses_to_present_an_incomplete_run_as_a_result(tmp_path):
    ledger = await incomplete_run(tmp_path)
    summary = summarise(ledger.path)
    with pytest.raises(IncompleteRun, match="token_ceiling"):
        summary.require_complete()
    with pytest.raises(IncompleteRun):
        summary.result()


async def test_the_label_survives_being_written_and_read_back(tmp_path):
    ledger = await incomplete_run(tmp_path)
    path = write_summary(summarise(ledger.path), ledger.directory)

    raw = json.loads(path.read_text())
    assert raw["complete"] is False and "TBD" in raw["derived"]["tokens_per_task"]

    again = read_summary(path)
    assert again.complete is False and again.incomplete_reason == "token_ceiling"
    with pytest.raises(IncompleteRun):
        again.require_complete()


async def test_a_finished_run_does_produce_numbers(tmp_path):
    clock = SteppingClock()
    ledger = ledger_for(tmp_path, clock=clock)
    config = config_for([f"t-{i}" for i in range(4)])
    with ledger:
        await Run(config, ledger, clock=clock).execute(Spender(cost=100, clock=clock, step=15.0))

    summary = summarise(ledger.path).require_complete()
    assert summary.complete and summary.incomplete_reason is None
    assert "warning" not in summary.as_file()
    derived = summary.result()
    assert derived["tokens_per_task"] == 100.0
    assert derived["attempts_per_task"] == 1.0
    assert derived["tasks_per_minute"] == 4.0  # four tasks over sixty seconds


async def test_a_summary_counts_failure_classes_and_which_pool_served_what(tmp_path):
    """Phase 6 reconstructs capacity from this, which is why `pool` is in the row."""
    ledger = ledger_for(tmp_path)
    config = config_for(["t-0", "t-1"])

    async def executor(context):
        if context.task_id == "t-1":
            context.record_failure(
                ProviderHTTPError(
                    status=429,
                    body=GOOGLE_DAILY_429,
                    provider="google-ai-studio",
                    model="gemini-3.8-flash",
                    pool="google-ai-studio#2",
                )
            )
        context.record(completion())
        return TaskResult()

    with ledger:
        await Run(config, ledger).execute(executor)

    summary = summarise(ledger.directory)
    assert summary.error_classes == {"quota_day": 1}
    assert summary.attempts_by_model == {
        "groq/openai/gpt-oss-20b[groq#1]": 2,
        "google-ai-studio/gemini-3.8-flash[google-ai-studio#2]": 1,
    }
    assert summary.attempts == 3 and summary.attempts_answered == 2


async def test_a_run_killed_outright_summarises_as_killed_and_never_as_finished(tmp_path):
    """The sixth reason: no `run_end` exists, because the process could not write one."""
    ledger = ledger_for(tmp_path)
    with ledger:
        ledger.append({"kind": "run_start", "run_id": "r-1", "agent": "A0", "tasks_declared": 6})
        ledger.append({"kind": "task", "run_id": "r-1", "task_id": "t-0", "status": "complete"})

    summary = summarise(ledger.path)
    assert summary.complete is False
    assert summary.incomplete_reason == IncompleteReason.KILLED
    assert summary.tasks_remaining == 5
    with pytest.raises(IncompleteRun, match="killed"):
        summary.require_complete()


def test_the_six_reasons_a_run_can_be_incomplete_stay_distinct():
    """They mean different things to a reader and must not collapse into one flag."""
    assert {r.value for r in IncompleteReason} == {
        "token_ceiling",
        "wall_clock_ceiling",
        "pools_exhausted",
        "client_fatal",
        "operator",
        "killed",
    }
