"""The local API, 7.1: a question against a named sandbox database, its trajectory, its result.

**Local only.** It binds `127.0.0.1` and nothing else (:data:`HOST`), because a public URL
carrying this project's keys lets any visitor spend the free tier the reserve run still needs
(constraint 79). What is deployed is the replay viewer; this serves the same page, from the
same files, with its Run button alive.

**What runs a question.** :class:`~query_pilot.agents.live.LiveA1` — A1's loop, unchanged, with
no reference and so no verdict — as A1 on `strong` or A2-cheap on `cheap-no-spillover`
(`agents/live.py`). Each question is a run of one task through :class:`~query_pilot.run.Run`,
held to `config/runs/api.toml`'s two ceilings at concurrency 1, so every trajectory is recorded
under `runs/api/<run_id>/` exactly as a measured one is: a ledger and a transcript.

**Poll, not stream.** ``GET /api/questions/{run_id}`` reads the ledger and the transcript from
disk each time — ordered by ``seq``, a half-written last line left for the next read — and
returns the trajectory the way the viewer lays one out (:func:`~query_pilot.viewer.
trajectory_view`). What it returns survives a restart, because it is only ever the record.

**What it will run, and how much.** Every figure below is derived, and `tests/test_api.py`
shows what each admits and refuses:

- **Databases**: the 20 working-set databases (:data:`DATABASES`), matched by exact name before
  any path is built. No path, no connection string, nothing from the 166 directories Spider
  ships beyond these.
- **One question at a time**, across the whole server (constraints 46, 64 and 91).
- **One pool**: the client this is served with holds `GROQ_API_KEY` alone, `groq#1`
  (:func:`live_client`), so it can never spend the other two Groq pools or Google.
- **A daily token cap per model** (:data:`DAILY_TOKEN_CAP`), counted from the API's own ledgers
  so a restart does not reset it, with every question's worst case set aside before it starts.
- **Per address**, :data:`PER_IP_QUESTIONS` questions an hour. On loopback every caller is the
  same address, so this is a second global limit, and the daily cap is what protects quota.

**Keys** reach no response: every body this returns is scrubbed of every key value in the
environment it was started from, and nothing here logs a request's headers or a key.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from query_pilot.agents.live import AGENTS, LIVE_TASK_ID, LiveA1, live_task
from query_pilot.agents.transcript import read_trajectories, transcript_path
from query_pilot.client.client import Client
from query_pilot.client.clock import Clock, SystemClock
from query_pilot.client.config import ClientConfig
from query_pilot.run import LEDGER_NAME, Run, RunConfig, RunLedger, new_run_id, read_rows
from query_pilot.sandbox import database_path
from query_pilot.viewer import CONTROLS, LABELS, controls_fired, trajectory_view

__all__ = [
    "DAILY_TOKEN_CAP",
    "DATABASES",
    "HOST",
    "PER_IP_QUESTIONS",
    "QUESTION_MAX_CHARS",
    "LiveClient",
    "create_app",
    "live_client",
    "serve",
]

log = logging.getLogger("query_pilot.api")

REPO: Final = Path(__file__).resolve().parents[2]
VIEWER: Final = REPO / "viewer"
DATABASE_ROOT: Final = REPO / "data" / "spider" / "database"
RUNS_ROOT: Final = REPO / "runs" / "api"
QUOTA_WALLS: Final = REPO / "runs" / "quota-walls.jsonl"
DECLARATION: Final = REPO / "config" / "runs" / "api.toml"

#: Loopback, and no option to change it. Constraint 79.
HOST: Final = "127.0.0.1"
#: The one key the API's client holds. `discover_pools` names it `groq#1`.
POOL_KEY: Final = "GROQ_API_KEY"

#: **The allowlist: the 20 working-set databases**, exactly as `results/a1-working.json` names
#: them — a committed working-set file, so deriving it never opens `splits/reserve.json`. The
#: working set covers all 20 of Spider dev's databases, so the reserve set's questions are asked
#: of these same databases; the API exposes databases, never a reserve task.
DATABASES: Final[tuple[str, ...]] = (
    "battle_death",
    "car_1",
    "concert_singer",
    "course_teach",
    "cre_Doc_Template_Mgt",
    "dog_kennels",
    "employee_hire_evaluation",
    "flight_2",
    "museum_visit",
    "network_1",
    "orchestra",
    "pets_1",
    "poker_player",
    "real_estate_properties",
    "singer",
    "student_transcripts_tracking",
    "tvshow",
    "voter_1",
    "world_1",
    "wta_1",
)

#: The longest question the API accepts. The longest working-set question is 152 characters;
#: at 1,000 the opening request is 9.1% of `PROMPT_CEILING_CHARS`. **A1's prompt ceiling never
#: stops the first turn** (constraint 52), so without this one typed question could exceed the
#: per-minute token bucket in a single request and end the run as `pools_exhausted` (46).
QUESTION_MAX_CHARS: Final = 1_000

#: Groq's tokens a day, per pool and per model, refilling continuously (constraint 95;
#: `config/providers.toml`).
PROVIDER_TOKENS_PER_DAY: Final = 200_000
#: What A0's working-set run spent (`results/a0-working.json`, `tokens.total`) — the reserve
#: run's agent over the same number of tasks, and so what the reserve run should need.
RESERVE_RUN_TOKENS: Final = 106_740
#: **The daily cap: 93,260 tokens per model in any rolling 24 hours.** With the API's spend held
#: under ``PROVIDER_TOKENS_PER_DAY - RESERVE_RUN_TOKENS``, the reserve run fits even on the same
#: pool on the same day. A rolling sum is conservative against Groq's refilling bucket.
DAILY_TOKEN_CAP: Final = PROVIDER_TOKENS_PER_DAY - RESERVE_RUN_TOKENS
#: One request at the prompt ceiling on 120b (`docs/escalation-a1-working.json`,
#: `projected_attempt_at_ceiling`; 9,565 on 20b). The guard checks between turns, so a question
#: can pass its token ceiling by at most this much, and the cap sets it aside.
WORST_ATTEMPT_TOKENS: Final = 9_777
DAY_S: Final = 86_400.0

#: **Ten questions an hour from one address.** Derived from 7.4's clip: A2-cheap repaired 30 of
#: 150 trajectories, so ten tries give a 1 - (120/150)**10 = 89.3% chance of at least one
#: repair — the largest burst this project has a use for. Only a submission counts; a poll or a
#: status read spends nothing.
PER_IP_QUESTIONS: Final = 10
PER_IP_WINDOW_S: Final = 3_600.0
#: How often the page asks. A turn averaged about 6.3 s on A1's measured run (34.94 s over 5.51
#: requests a task), so one second shows each step within a second of its being written.
POLL_INTERVAL_S: Final = 1.0

RUN_ID: Final = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
_KEY_NAME: Final = re.compile(r"[A-Z0-9_]*API_KEY(?:_\d+)?")
_REDACTED: Final = "[redacted]"

SURFACE: Final = "local API"
NOT_SCORED: Final = "no reference: not scored"
#: Replaces the replay line at the top of the page, which says no model is called from it.
MASTHEAD: Final = (
    "Replays runs already measured, and runs new questions through the local API on this machine."
)
NOTE: Final = (
    "Runs a new trajectory on this machine through the local API. A typed question has no "
    "reference query, so its answer is not scored and joins no committed figure."
)
ROWS_RAN: Final = (
    "the rows the final statement returned when the local API ran it, shown the way the agent "
    "is shown rows"
)


# -- the client -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiveClient:
    """A client, the pools it holds, and every key value to keep out of a response."""

    client: Client
    pools: tuple[str, ...]
    secrets: tuple[str, ...]


def key_values(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Every credential value in ``environ``, whether or not the API uses it."""
    return tuple(
        sorted(
            {v.strip() for k, v in environ.items() if _KEY_NAME.fullmatch(k) and v and v.strip()}
        )
    )


def live_client(environ: Mapping[str, str] | None = None, **kwargs: Any) -> LiveClient:
    """A client on `GROQ_API_KEY` alone — pool `groq#1` — and on nothing else.

    It is handed an environment holding that one key, so `groq#2`, `groq#3` and Google cannot
    be reached from here whatever else is set. **Keys are not loaded for you** (constraint 45):
    `set -a && . ./.env && set +a` first.
    """
    env = dict(os.environ if environ is None else environ)
    key = (env.get(POOL_KEY) or "").strip()
    kwargs.setdefault("quota_walls", QUOTA_WALLS)
    client = Client.from_config(environ={POOL_KEY: key} if key else {}, **kwargs)
    pools = tuple(credential.pool for credential in client.registry.pools("groq"))
    return LiveClient(client=client, pools=pools, secrets=key_values(env))


def role_model(config: ClientConfig, role: str) -> str:
    """The model a role spends on, read from the committed configuration — never named here.

    Refuses a role with spillover: its spend could land on a second model this cap never sees.
    """
    spec = config.roles[role]
    if spec.spillover:
        raise ValueError(f"role {role!r} spills over to {spec.spillover}; the API needs one model")
    return config.endpoints[spec.endpoint].model


def scrub(value: Any, secrets: Sequence[str]) -> Any:
    """``value`` with every occurrence of every secret replaced, in every string it holds."""
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, _REDACTED)
        return value
    if isinstance(value, Mapping):
        return {scrub(k, secrets): scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(v, secrets) for v in value]
    return value


# -- spend and limits -----------------------------------------------------------------------------


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def spend(runs_root: Path, since: datetime) -> dict[str, list[tuple[datetime, int]]]:
    """Every attempt the API's own runs recorded after ``since``, by model, oldest first."""
    by_model: dict[str, list[tuple[datetime, int]]] = {}
    for ledger in sorted(runs_root.glob(f"*/{LEDGER_NAME}")):
        for row in read_rows(ledger):
            if row.get("kind") != "attempt":
                continue
            at = _parse(str(row["recorded_at"]))
            if at <= since:
                continue
            tokens = int(row.get("prompt_tokens") or 0) + int(row.get("completion_tokens") or 0)
            by_model.setdefault(str(row["model"]), []).append((at, tokens))
    for rows in by_model.values():
        rows.sort()
    return by_model


@dataclass(frozen=True, slots=True)
class Refusal:
    status: int
    error: str
    retry_after_s: float | None = None


class Limits:
    """Per address, and per model per day. The one-at-a-time rule is the service's own."""

    def __init__(
        self,
        clock: Clock,
        runs_root: Path,
        *,
        reservation: int,
        cap: int = DAILY_TOKEN_CAP,
        per_ip: int = PER_IP_QUESTIONS,
        window_s: float = PER_IP_WINDOW_S,
    ) -> None:
        if reservation > cap:
            raise ValueError("a question's worst case is above the daily cap; nothing could run")
        self.clock = clock
        self.runs_root = runs_root
        self.reservation = reservation
        self.cap = cap
        self.per_ip = per_ip
        self.window_s = window_s
        self._starts: dict[str, deque[float]] = {}

    def spent(self) -> dict[str, int]:
        since = self.clock.now_utc() - timedelta(seconds=DAY_S)
        by_model = spend(self.runs_root, since)
        return {model: sum(t for _, t in rows) for model, rows in by_model.items()}

    def check(self, address: str, model: str) -> Refusal | None:
        now = self.clock.monotonic()
        starts = self._starts.setdefault(address, deque())
        while starts and now - starts[0] >= self.window_s:
            starts.popleft()
        if len(starts) >= self.per_ip:
            return Refusal(
                429,
                f"at most {self.per_ip} questions an hour from one address",
                round(self.window_s - (now - starts[0]), 3),
            )
        wall = self.clock.now_utc()
        rows = spend(self.runs_root, wall - timedelta(seconds=DAY_S)).get(model, [])
        spent = sum(t for _, t in rows)
        if spent + self.reservation > self.cap:
            # When enough of the day's spend has aged out for one more worst case to fit.
            excess = spent + self.reservation - self.cap
            released = 0
            retry = DAY_S
            for at, tokens in rows:
                released += tokens
                if released >= excess:
                    retry = (at + timedelta(seconds=DAY_S) - wall).total_seconds()
                    break
            return Refusal(
                429,
                f"the daily cap for {model} is reached: {spent:,} of {self.cap:,} tokens in the "
                f"last 24 hours, and a question sets {self.reservation:,} aside",
                round(max(retry, 0.0), 3),
            )
        return None

    def admit(self, address: str) -> None:
        self._starts.setdefault(address, deque()).append(self.clock.monotonic())


# -- reading one live run -------------------------------------------------------------------------


def _final(detail: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if detail is None:
        return None
    sql = detail.get("sql")
    result = detail.get("result") or {}
    if not sql:
        note, rows = LABELS["no_statement"], None
    elif result.get("ok"):
        note, rows = ROWS_RAN, {"text": result.get("text")}
    else:
        note, rows = f"the final statement did not run: {result.get('error')}", None
    return {
        "sql": sql,
        "rows": rows,
        "rows_note": note,
        "candidate_rows": result.get("rows_returned"),
    }


def _controls(view: Mapping[str, Any] | None, detail: Mapping[str, Any] | None) -> list[dict]:
    fired: list[dict[str, Any]] = []
    for step in (view or {}).get("steps", []):
        if step["kind"] == "call":
            for number in step["controls"]:
                fired.append(_control(number, step["turn"], step["tool"]))
    if detail is not None and detail.get("result") is not None:
        sql = {"sql": detail.get("sql") or ""}
        for number in controls_fired("execute_sql", sql, detail["result"]):
            fired.append(_control(number, None, "final statement"))
    return fired


def _control(number: int, turn: int | None, tool: str) -> dict[str, Any]:
    return {"control": number, "name": CONTROLS[number], "turn": turn, "tool": tool}


def _shown(path: Path) -> str:
    """A path as the repository names it, never as this machine's filesystem does."""
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return f"{path.parent.name}/{path.name}"


def question_view(
    runs_root: Path, run_id: str, *, running: bool, clock: Clock
) -> dict[str, Any] | None:
    """One live run as the page lays it out, read from its ledger and transcript. ``None`` if
    ``run_id`` is not a run this API recorded."""
    directory = runs_root / run_id
    rows = list(read_rows(directory / LEDGER_NAME))
    starts = [r for r in rows if r.get("kind") == "run_start"]
    if not starts or starts[0]["declared"]["params"].get("surface") != "api":
        return None
    start = starts[0]
    attempts = [r for r in rows if r.get("kind") == "attempt"]
    tasks = [r for r in rows if r.get("kind") == "task"]
    ends = [r for r in rows if r.get("kind") == "run_end"]
    task = tasks[-1] if tasks else None
    end = ends[-1] if ends else None

    brackets = read_trajectories(transcript_path(directory, LIVE_TASK_ID), while_written=True)
    view = trajectory_view(brackets[-1]) if brackets else None

    if task is not None:
        status = "complete" if task["status"] == "complete" else "failed"
    elif end is not None:
        status = "stopped"
    else:
        status = "running" if running else "cut off"

    detail = task["detail"] if task is not None and task["status"] == "complete" else None
    params = start["declared"]["params"]
    last = attempts[-1] if attempts else None
    if task is not None:
        elapsed = task["elapsed_s"]
    else:
        elapsed = (clock.now_utc() - _parse(start["started_at"])).total_seconds()
    footer = {
        "model": f"{last['provider']}/{last['model']}" if last else None,
        "turns": (view or {}).get("end", {}).get("turns"),
        "tool_calls": (view or {}).get("end", {}).get("tool_calls"),
        "tokens_in": sum(int(a.get("prompt_tokens") or 0) for a in attempts),
        "tokens_out": sum(int(a.get("completion_tokens") or 0) for a in attempts),
        "elapsed_s": elapsed,
        "termination": (view or {}).get("end", {}).get("termination"),
        "verdict": NOT_SCORED,
        "controls": _controls(view, detail),
    }
    failure = None
    if task is not None and task["status"] != "complete":
        failure = {
            "error_class": task.get("error_class"),
            "exception": task["detail"].get("exception"),
            "message": task["detail"].get("message"),
        }
    return {
        "run_id": run_id,
        "status": status,
        "incomplete_reason": end.get("incomplete_reason") if end else None,
        "agent": start["agent"],
        "role": params.get("role"),
        "database": (view or {}).get("database"),
        "question": (view or {}).get("question"),
        "system_prompt": (view or {}).get("system_prompt"),
        "steps": (view or {}).get("steps", []),
        "end": (view or {}).get("end"),
        "final": _final(detail),
        "footer": footer,
        "failure": failure,
        "attempts": len(attempts),
        "ledger": _shown(directory / LEDGER_NAME),
        "scored": False,
    }


# -- the service ----------------------------------------------------------------------------------


class Question(BaseModel):
    """What a submission may carry, and nothing else."""

    model_config = ConfigDict(extra="forbid", strict=True)

    database: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=QUESTION_MAX_CHARS)
    agent: Literal["A1", "A2-cheap"] = "A1"


class _Service:
    def __init__(
        self,
        *,
        client: Any,
        database_root: Path,
        runs_root: Path,
        declaration: RunConfig,
        client_config: ClientConfig,
        clock: Clock,
        secrets: tuple[str, ...],
        pools: tuple[str, ...],
        rng: random.Random,
    ) -> None:
        self.client = client
        self.database_root = database_root
        self.runs_root = runs_root
        self.declaration = declaration
        self.clock = clock
        self.secrets = secrets
        self.pools = pools
        self.rng = rng
        self.models = {name: role_model(client_config, spec.role) for name, spec in AGENTS.items()}
        self.limits = Limits(
            clock, runs_root, reservation=declaration.token_ceiling + WORST_ATTEMPT_TOKENS
        )
        self.current: asyncio.Task[None] | None = None
        self.current_run_id: str | None = None

    def json(self, payload: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(scrub(payload, self.secrets), status_code=status)

    @property
    def running(self) -> str | None:
        if self.current is not None and not self.current.done():
            return self.current_run_id
        return None

    def status(self) -> dict[str, Any]:
        spent = self.limits.spent()
        return {
            "surface": SURFACE,
            "live": True,
            "masthead": MASTHEAD,
            "note": NOTE,
            "databases": list(DATABASES),
            "agents": {
                name: {"role": spec.role, "model": self.models[name]}
                for name, spec in AGENTS.items()
            },
            "pools": list(self.pools),
            "limits": {
                "concurrency": 1,
                "question_max_chars": QUESTION_MAX_CHARS,
                "questions_per_address_per_hour": PER_IP_QUESTIONS,
                "daily_token_cap_per_model": DAILY_TOKEN_CAP,
                "set_aside_per_question": self.limits.reservation,
                "token_ceiling": self.declaration.token_ceiling,
                "wall_clock_ceiling_s": self.declaration.wall_clock_ceiling_s,
            },
            "spent_last_24h": {model: spent.get(model, 0) for model in self.models.values()},
            "running": self.running,
            "poll_interval_s": POLL_INTERVAL_S,
        }

    def new_run_id(self) -> str:
        while True:
            run_id = new_run_id(self.clock, self.rng)
            if not (self.runs_root / run_id).exists():
                return run_id

    async def run(self, run_id: str, db_id: str, question: str, agent_name: str) -> None:
        spec = AGENTS[agent_name]
        task = live_task(db_id, question)
        declared = self.declaration
        config = RunConfig.start(
            agent_name,
            [task.task_id],
            token_ceiling=declared.token_ceiling,
            wall_clock_ceiling_s=declared.wall_clock_ceiling_s,
            run_id=run_id,
            concurrency=declared.concurrency,
            params={
                **declared.params,
                "role": spec.role,
                "rejected_generation": spec.rejected_generation,
            },
        )
        ledger = RunLedger(run_id, root=self.runs_root, clock=self.clock)
        run = Run(config, ledger, clock=self.clock)
        agent = LiveA1(
            self.client,
            [task],
            self.database_root,
            ledger.directory,
            role=spec.role,
            rejected_generation=spec.rejected_generation,
        )
        try:
            with ledger:
                report = await run.execute(agent)
            log.info(
                "run %s: %s, %d tasks complete, %d failed, %d attempts, %d tokens",
                run_id,
                report.status,
                report.tasks_complete,
                report.tasks_failed,
                report.attempts,
                report.total_tokens,
            )
        except Exception as error:
            # A fault in this machinery rather than a stopping condition: the ledger reads as
            # cut off, which is the truth. Named here without its traceback, and scrubbed.
            log.error(
                "run %s raised %s: %s",
                run_id,
                type(error).__name__,
                scrub(str(error), self.secrets),
            )
        finally:
            agent.close()


def create_app(
    *,
    client: Any,
    database_root: Path | str = DATABASE_ROOT,
    runs_root: Path | str = RUNS_ROOT,
    viewer_root: Path | str = VIEWER,
    declaration: Path | str = DECLARATION,
    client_config: ClientConfig | None = None,
    clock: Clock | None = None,
    secrets: Iterable[str] = (),
    pools: Sequence[str] = (),
    rng: random.Random | None = None,
    owns_client: bool = False,
) -> FastAPI:
    """The API and the viewer it serves. ``client`` is anything with A1's ``complete``."""
    service = _Service(
        client=client,
        database_root=Path(database_root),
        runs_root=Path(runs_root),
        declaration=RunConfig.load(declaration, [LIVE_TASK_ID]),
        client_config=client_config or ClientConfig.load(),
        clock=clock or SystemClock(),
        secrets=tuple(s for s in secrets if s),
        pools=tuple(pools),
        rng=rng or random.Random(),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
        yield
        if service.current is not None and not service.current.done():
            # The run records itself as stopped by an operator (`Run.execute`).
            service.current.cancel()
            await asyncio.gather(service.current, return_exceptions=True)
        if owns_client:
            await client.aclose()

    # FastAPI's own documentation pages load their scripts from a CDN; none is served.
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service

    @app.middleware("http")
    async def revalidate(request: Request, call_next):  # type: ignore[no-untyped-def]
        # The page is served from the working tree; a browser holding yesterday's app.js would
        # run it against today's API. Revalidating costs a 304.
        reply = await call_next(request)
        reply.headers["Cache-Control"] = "no-cache"
        return reply

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        # The fields only, never the values sent: nothing a caller posts is echoed back.
        fields = sorted({".".join(str(part) for part in e["loc"][1:]) for e in exc.errors()})
        return service.json(
            {
                "error": "send JSON with a database, a question and an agent (A1 or A2-cheap)",
                "fields": fields,
            },
            422,
        )

    @app.get("/api/status")
    async def status() -> JSONResponse:
        return service.json(service.status())

    @app.post("/api/questions")
    async def ask(body: Question, request: Request) -> JSONResponse:
        if not body.question.strip() or "\x00" in body.question:
            return service.json({"error": "the question is empty or holds a NUL character"}, 422)
        if body.database not in DATABASES:
            return service.json(
                {
                    "error": "unknown database: the API serves the working-set databases only",
                    "databases": list(DATABASES),
                },
                404,
            )
        if not database_path(service.database_root, body.database).exists():
            return service.json({"error": "that database is not on this machine"}, 503)
        running = service.running
        if running is not None:
            return service.json(
                {"error": "one question at a time; poll the one running", "running": running},
                429,
            )
        address = request.client.host if request.client else "unknown"
        refusal = service.limits.check(address, service.models[body.agent])
        if refusal is not None:
            return service.json(
                {"error": refusal.error, "retry_after_s": refusal.retry_after_s}, refusal.status
            )
        service.limits.admit(address)
        run_id = service.new_run_id()
        service.current_run_id = run_id
        service.current = asyncio.create_task(
            service.run(run_id, body.database, body.question, body.agent)
        )
        log.info("run %s admitted: %s on %s", run_id, body.agent, body.database)
        return service.json({"run_id": run_id, "poll": f"api/questions/{run_id}"}, 202)

    @app.get("/api/questions/{run_id}")
    async def poll(run_id: str) -> JSONResponse:
        if not RUN_ID.fullmatch(run_id):
            return service.json({"error": "not a run this API recorded"}, 404)
        running = service.running == run_id
        view = question_view(service.runs_root, run_id, running=running, clock=service.clock)
        if view is None:
            if running:
                return service.json({"run_id": run_id, "status": "starting", "steps": []})
            return service.json({"error": "not a run this API recorded"}, 404)
        return service.json(view)

    # Last, so every /api route above is matched first. `html=True` serves index.html at `/`.
    app.mount("/", StaticFiles(directory=Path(viewer_root), html=True), name="viewer")
    return app


def serve(app: FastAPI, *, port: int = 8765) -> None:
    """Run ``app`` on loopback. There is no host argument, on purpose (constraint 79)."""
    import uvicorn

    uvicorn.run(app, host=HOST, port=port, access_log=False, log_level="info")
