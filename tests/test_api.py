"""7.1: the local API — what it runs, what it refuses, what it records, and what it never shows.

**No test here makes a live API call and none needs a key.** A question is driven through the
real app with FastAPI's `TestClient` — no socket — against a scripted client, or against the
real `Client` over `conftest.FakeHttp` where what is under test is the request the client would
send. The page's JavaScript has no runner here; what decides whether its Run button can come
alive is checked in its source and against a static copy of `viewer/`, which is what a deployed
host serves.

Four things carry the weight. **The allowlist and the caps**, each at its two edges, because a
limit no request can reach is not a limit. **The record**: every live trajectory lands under
`runs/api/` as a run of one task, readable back by the viewer's own `trajectory_view`, and
never compared with anything. **The key**, which reaches no response, log, transcript or
ledger. And **the Run button's gate**, which a deployed copy of the page must never pass.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import random
import re
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from conftest import FakeClock, FakeHttp, groq_reply, response
from query_pilot import api
from query_pilot.agents.a1 import (
    FAIL_TASK,
    PROMPT_CEILING_CHARS,
    SCORE_UNSOLVED,
    build_prompt,
    conversation_chars,
)
from query_pilot.agents.live import AGENTS, LIVE_TASK_ID
from query_pilot.agents.transcript import (
    read_trajectories,
    read_transcript,
    replay,
    transcript_path,
)
from query_pilot.client.config import ClientConfig
from query_pilot.client.types import Completion, ToolCall
from query_pilot.run import RunConfig, read_rows
from query_pilot.sandbox import Sandbox, database_path
from query_pilot.viewer import INPUTS, trajectory_view

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "viewer" / "app.js"
KEYS = re.compile(r"gsk_[A-Za-z0-9]{8,}|AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{20,}|API_KEY|\.env\b")
PROHIBITED = (
    "ladder", "rung", "harness", "judge", "golden set", "ndcg", "relevance", "held-out",
    "bootstrap", "ablation", "attribution", "configuration", "residual", "hardened",
    "<pending>", "[measured]", "correct", "incorrect",
)  # fmt: skip
SENTINEL = "gsk_SENTINELkeyONE0000000000000000"
OTHERS = (
    "gsk_SENTINELkeyTWO000000000000000",
    "gsk_SENTINELkeyTHREE0000000000000",
    "AIza" + "S" * 35,
)
MODELS = {
    role: api.role_model(ClientConfig.load(), role) for role in ("strong", "cheap-no-spillover")
}
ANSWER = "SELECT name FROM singer ORDER BY age DESC"


# --- material ------------------------------------------------------------------------------------


def _database(path: Path) -> None:
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE singer (id INT PRIMARY KEY, name TEXT NOT NULL, age INT);
            INSERT INTO singer VALUES (1, 'Joe', 30), (2, 'Rose', 41);
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def database_root(tmp_path: Path) -> Path:
    root = tmp_path / "database"
    _database(database_path(root, "concert_singer"))
    # A database that exists on disk but is not on the allowlist: Spider ships 166 of them.
    _database(database_path(root, "academic"))
    return root


def call(name: str, **arguments) -> ToolCall:
    return ToolCall(id=f"call-{name}-{len(arguments)}", name=name, arguments=arguments)


class Scripted:
    """Answers from a script, and remembers every conversation it was handed.

    ``before`` runs ahead of each answer, which is how a test advances the clock or holds a
    trajectory mid-flight while it polls.
    """

    def __init__(self, *responses, tokens=(400, 20), before=None) -> None:
        self.responses = list(responses)
        self.handed: list[list] = []
        self.roles: list[str] = []
        self.tokens = tokens
        self.before = before

    async def complete(self, role, messages, tools=None, *, max_output_tokens=512, **kwargs):
        self.roles.append(role)
        self.handed.append(list(messages))
        if self.before is not None:
            await self.before(len(self.handed))
        answer = self.responses.pop(0) if self.responses else ""
        if isinstance(answer, Exception):
            raise answer
        text, calls = answer if isinstance(answer, tuple) else (answer, ())
        prompt, completion = self.tokens
        return Completion(
            text=text,
            tool_calls=tuple(calls),
            prompt_tokens=prompt,
            completion_tokens=completion,
            provider="groq",
            model=MODELS[role],
            pool="groq#1",
            latency_s=0.5,
            finish_reason="tool_calls" if calls else "stop",
        )


def held(gate: threading.Event, after: int):
    """A ``before`` hook that holds every answer after the first ``after`` until ``gate`` opens."""

    async def hook(n: int) -> None:
        if n > after:
            while not gate.is_set():
                await asyncio.sleep(0.005)

    return hook


def build(tmp_path: Path, database_root: Path, client, **kwargs) -> FastAPI:
    kwargs.setdefault("clock", FakeClock())
    kwargs.setdefault("pools", ("groq#1",))
    return api.create_app(
        client=client,
        database_root=database_root,
        runs_root=tmp_path / "runs" / "api",
        rng=random.Random(20260913),
        **kwargs,
    )


def ask(http: TestClient, question: str = "Who are the singers, oldest first?", **body):
    payload = {"database": "concert_singer", "question": question, "agent": "A1", **body}
    return http.post("/api/questions", json=payload)


def settle(http: TestClient, run_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = http.get(f"/api/questions/{run_id}").json()
        if view["status"] not in ("running", "starting"):
            return view
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not finish in {timeout} s")


def run_directory(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / "runs" / "api" / run_id


def rows(tmp_path: Path, run_id: str) -> list[dict]:
    return list(read_rows(run_directory(tmp_path, run_id) / "ledger.jsonl"))


def three_turns() -> Scripted:
    return Scripted(
        ("", [call("list_tables")]),
        ("", [call("describe_table", table="singer")]),
        ("", [call("execute_sql", sql=ANSWER)]),
        ANSWER,
    )


# --- the allowlist and what a submission may carry -----------------------------------------------


def test_the_allowlist_is_the_twenty_working_set_databases() -> None:
    measured = json.loads((REPO / "results" / "a1-working.json").read_text())
    shown = json.loads((REPO / "viewer" / "data" / "index.json").read_text())
    working = sorted({task["db_id"] for task in measured["tasks"]})
    assert list(api.DATABASES) == working == sorted(shown["databases"])
    assert len(api.DATABASES) == 20


def test_nothing_the_api_runs_on_names_the_reserve_set() -> None:
    # Every string in the code, docstrings aside: a path to the reserve set would be one of them.
    for path in (REPO / "src/query_pilot/api.py", REPO / "src/query_pilot/agents/live.py",
                 REPO / "scripts/serve_api.py"):  # fmt: skip
        tree = ast.parse(path.read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            literal = isinstance(node, ast.Constant) and isinstance(node.value, str)
            if literal and id(node) not in docstrings:
                assert "reserve" not in node.value.casefold(), (path.name, node.value)


@pytest.mark.parametrize(
    "name",
    [
        "academic",  # on disk, not on the allowlist
        "../concert_singer",
        "concert_singer/../academic",
        "/etc/passwd",
        "concert_singer.sqlite",
        "CONCERT_SINGER",
        " concert_singer",
        "concert_singer ",
        "file:concert_singer?mode=rw",
        "sqlite:///concert_singer",
        "c\u043encert_singer",  # a Cyrillic o, written as an escape
        "concert_singer\x00",
    ],
)
def test_a_database_off_the_allowlist_is_refused_before_any_path_is_built(
    tmp_path, database_root, monkeypatch, name
) -> None:
    built: list[str] = []
    real = api.database_path
    monkeypatch.setattr(api, "database_path", lambda root, db: built.append(db) or real(root, db))
    client = Scripted(ANSWER)
    with TestClient(build(tmp_path, database_root, client)) as http:
        reply = ask(http, database=name)
    assert reply.status_code == 404
    assert built == [] and client.handed == []
    body = reply.text
    assert str(database_root) not in body and str(tmp_path) not in body
    assert reply.json()["databases"] == list(api.DATABASES)


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"question": ""}, "question"),
        ({"question": "x" * (api.QUESTION_MAX_CHARS + 1)}, "question"),
        ({"question": 7}, "question"),
        ({"database": 3}, "database"),
        ({"database": "x" * 65}, "database"),
        ({"agent": "A0"}, "agent"),
        ({"agent": "A2"}, "agent"),
        ({"agent": "cheap"}, "agent"),
        ({"path": "/etc/passwd"}, "path"),
        ({"connection": "sqlite:///x.db"}, "connection"),
    ],
)
def test_a_malformed_submission_is_refused_without_echoing_it(
    tmp_path, database_root, body, field
) -> None:
    client = Scripted(ANSWER)
    with TestClient(build(tmp_path, database_root, client)) as http:
        reply = ask(http, **body)
    assert reply.status_code == 422
    assert field in reply.json()["fields"]
    for value in body.values():
        if isinstance(value, str) and len(value) > 8:
            assert value not in reply.text
    assert client.handed == []


@pytest.mark.parametrize("question", ["   ", "a\x00b"])
def test_a_blank_question_or_a_nul_is_refused(tmp_path, database_root, question) -> None:
    with TestClient(build(tmp_path, database_root, Scripted(ANSWER))) as http:
        assert ask(http, question=question).status_code == 422


def test_a_question_at_the_cap_is_admitted(tmp_path, database_root) -> None:
    with TestClient(build(tmp_path, database_root, Scripted(ANSWER))) as http:
        reply = ask(http, question="x" * api.QUESTION_MAX_CHARS)
        assert reply.status_code == 202
        assert settle(http, reply.json()["run_id"])["status"] == "complete"


def test_the_question_cap_sits_above_every_working_set_question_and_far_under_the_ceiling() -> None:
    questions = [
        json.loads(p.read_text())["question"] for p in (REPO / "viewer/data/tasks").glob("*.json")
    ]
    assert len(questions) == 150 and max(map(len, questions)) == 152
    assert max(map(len, questions)) < api.QUESTION_MAX_CHARS
    longest_name = max(api.DATABASES, key=len)
    opening = conversation_chars(build_prompt(longest_name, "x" * api.QUESTION_MAX_CHARS))
    assert opening / PROMPT_CEILING_CHARS < 0.10


def test_the_two_agents_are_declared_as_their_measured_runs_were() -> None:
    assert AGENTS["A1"].role == "strong" and AGENTS["A1"].rejected_generation == FAIL_TASK
    assert AGENTS["A2-cheap"].role == "cheap-no-spillover"
    assert AGENTS["A2-cheap"].rejected_generation == SCORE_UNSOLVED
    config = ClientConfig.load()
    assert MODELS == {"strong": "openai/gpt-oss-120b", "cheap-no-spillover": "openai/gpt-oss-20b"}
    # `cheap` spills to Google (constraint 89); the API refuses any role that reaches two models.
    with pytest.raises(ValueError, match="spills over"):
        api.role_model(config, "cheap")


# --- the record ----------------------------------------------------------------------------------


def test_a_question_is_a_run_of_one_task_recorded_like_any_other(tmp_path, database_root) -> None:
    client = three_turns()
    with TestClient(build(tmp_path, database_root, client)) as http:
        reply = ask(http)
        assert reply.status_code == 202
        run_id = reply.json()["run_id"]
        assert reply.json()["poll"] == f"api/questions/{run_id}"
        view = settle(http, run_id)

    ledger = rows(tmp_path, run_id)
    start = next(r for r in ledger if r["kind"] == "run_start")
    declared = RunConfig.load(api.DECLARATION, [LIVE_TASK_ID])
    assert start["declared"] == {
        "agent": "A1",
        "task_ids": [LIVE_TASK_ID],
        "token_ceiling": declared.token_ceiling,
        "wall_clock_ceiling_s": declared.wall_clock_ceiling_s,
        "concurrency": 1,
        "params": {"surface": "api", "role": "strong", "rejected_generation": FAIL_TASK},
    }
    attempts = [r for r in ledger if r["kind"] == "attempt"]
    assert [a["turn"] for a in attempts] == [1, 2, 3, 4]
    (task,) = [r for r in ledger if r["kind"] == "task"]
    assert task["status"] == "complete" and task["task_id"] == LIVE_TASK_ID
    assert task["detail"]["scored"] is False
    assert "solved" not in task["detail"] and "reference_sql" not in task["detail"]
    assert next(r for r in ledger if r["kind"] == "run_end")["status"] == "complete"
    assert client.roles == ["strong"] * 4

    # Replayable exactly as a measured transcript is, and laid out by the viewer's own reader.
    path = transcript_path(run_directory(tmp_path, run_id), LIVE_TASK_ID)
    events = read_transcript(path)
    for turn, handed in enumerate(client.handed, start=1):
        assert replay(events, turn=turn) == handed
    laid_out = trajectory_view(read_trajectories(path)[-1])
    assert view["steps"] == laid_out["steps"] and view["end"] == laid_out["end"]
    assert [s["tool"] for s in view["steps"] if s["kind"] == "call"] == [
        "list_tables", "describe_table", "execute_sql",
    ]  # fmt: skip

    assert view["status"] == "complete" and view["scored"] is False
    assert view["final"]["sql"] == ANSWER and view["final"]["candidate_rows"] == 2
    assert "Rose" in view["final"]["rows"]["text"]
    footer = view["footer"]
    assert footer["verdict"] == api.NOT_SCORED
    assert (footer["tokens_in"], footer["tokens_out"]) == (1600, 80)
    assert footer["model"] == "groq/openai/gpt-oss-120b"
    assert (footer["turns"], footer["tool_calls"], footer["termination"]) == (4, 3, "answer")


def test_a2_cheap_runs_on_the_cheap_role_under_its_own_refusal_rule(
    tmp_path, database_root
) -> None:
    client = Scripted(ANSWER)
    with TestClient(build(tmp_path, database_root, client)) as http:
        run_id = ask(http, agent="A2-cheap").json()["run_id"]
        view = settle(http, run_id)
    start = next(r for r in rows(tmp_path, run_id) if r["kind"] == "run_start")
    assert start["agent"] == "A2-cheap"
    assert start["declared"]["params"]["role"] == "cheap-no-spillover"
    assert start["declared"]["params"]["rejected_generation"] == SCORE_UNSOLVED
    assert client.roles == ["cheap-no-spillover"]
    assert view["footer"]["model"] == "groq/openai/gpt-oss-20b"


def test_a_live_question_executes_its_final_statement_once_and_nothing_else(
    tmp_path, database_root, monkeypatch
) -> None:
    ran: list[tuple[str, str]] = []
    execute, guarded = Sandbox.execute, Sandbox.execute_guarded
    monkeypatch.setattr(
        Sandbox, "execute", lambda s, db, sql: ran.append(("plain", sql)) or execute(s, db, sql)
    )
    monkeypatch.setattr(
        Sandbox,
        "execute_guarded",
        lambda s, db, sql: ran.append(("guarded", sql)) or guarded(s, db, sql),
    )
    with TestClient(build(tmp_path, database_root, Scripted(ANSWER))) as http:
        settle(http, ask(http).json()["run_id"])
    # One statement, through the guarded path (which opens the connection through `execute`),
    # and it is the answer. There is no reference to run.
    assert ran == [("guarded", ANSWER), ("plain", ANSWER)]


def test_a_write_in_a_final_statement_is_refused_and_the_database_is_untouched(
    tmp_path, database_root
) -> None:
    # Opens with WITH, so it passes validation and the opening-keyword guard; the read-only
    # connection is what refuses it, and the footer names control 1 on the final statement.
    write = "WITH x AS (SELECT 1) DELETE FROM singer"
    with TestClient(build(tmp_path, database_root, Scripted(write))) as http:
        view = settle(http, ask(http).json()["run_id"])
    assert view["final"]["rows"] is None
    assert "did not run" in view["final"]["rows_note"]
    assert {"control": 1, "turn": None, "tool": "final statement"}.items() <= next(
        c for c in view["footer"]["controls"] if c["tool"] == "final statement"
    ).items()
    conn = sqlite3.connect(database_path(database_root, "concert_singer"))
    assert conn.execute("SELECT count(*) FROM singer").fetchone() == (2,)
    conn.close()


def test_a_destructive_tool_call_is_refused_by_control_4_and_shown(tmp_path, database_root) -> None:
    client = Scripted(("", [call("execute_sql", sql="DROP TABLE singer")]), ANSWER)
    with TestClient(build(tmp_path, database_root, client)) as http:
        view = settle(http, ask(http).json()["run_id"])
    (step,) = [s for s in view["steps"] if s["kind"] == "call"]
    assert step["ok"] is False and step["controls"] == [4]
    assert {"control": 4, "turn": 1, "tool": "execute_sql"}.items() <= view["footer"]["controls"][
        0
    ].items()


def test_a_transcript_read_while_it_is_written_leaves_a_torn_last_line(tmp_path) -> None:
    path = tmp_path / "question.jsonl"
    path.write_text(
        json.dumps({"kind": "message", "seq": 2}) + "\n"
        + json.dumps({"kind": "start", "seq": 1}) + "\n"
        + '{"kind": "mess'
    )  # fmt: skip
    assert [e["seq"] for e in read_transcript(path, while_written=True)] == [1, 2]
    with pytest.raises(json.JSONDecodeError):
        read_transcript(path)


def test_polling_shows_a_trajectory_while_it_is_written(tmp_path, database_root) -> None:
    gate = threading.Event()
    client = Scripted(("", [call("list_tables")]), ANSWER, before=held(gate, after=1))
    with TestClient(build(tmp_path, database_root, client)) as http:
        run_id = ask(http).json()["run_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            view = http.get(f"/api/questions/{run_id}").json()
            if any(s["kind"] == "call" for s in view.get("steps", [])):
                break
            time.sleep(0.01)
        assert view["status"] == "running" and view["final"] is None
        assert view["steps"][0]["tool"] == "list_tables"
        gate.set()
        assert settle(http, run_id)["status"] == "complete"


def test_a_live_run_reads_back_the_same_after_a_restart(tmp_path, database_root) -> None:
    with TestClient(build(tmp_path, database_root, three_turns())) as http:
        run_id = ask(http).json()["run_id"]
        first = settle(http, run_id)
    with TestClient(build(tmp_path, database_root, Scripted())) as http:
        again = http.get(f"/api/questions/{run_id}").json()
    first["footer"].pop("elapsed_s")
    again["footer"].pop("elapsed_s")
    assert again == first


def test_only_a_run_this_api_recorded_can_be_read(tmp_path, database_root) -> None:
    # A measured run's ledger has no `surface = "api"`; a malformed ID never reaches the disk.
    other = run_directory(tmp_path, "20260910-024454-1f69bc")
    other.mkdir(parents=True)
    start = {"kind": "run_start", "agent": "A1", "started_at": "2026-09-10T02:44:54+00:00",
             "declared": {"params": {"split": "working"}}}  # fmt: skip
    (other / "ledger.jsonl").write_text(json.dumps(start) + "\n")
    with TestClient(build(tmp_path, database_root, Scripted())) as http:
        assert http.get("/api/questions/20260910-024454-1f69bc").status_code == 404
        assert http.get("/api/questions/20990101-000000-000000").status_code == 404
        assert http.get("/api/questions/..%2F..%2Fpyproject.toml").status_code == 404
        assert http.get("/api/questions/not-a-run").status_code == 404


def test_nothing_that_builds_a_committed_file_reads_the_apis_runs() -> None:
    assert not any(value.startswith("runs") for value in INPUTS.values())
    readers = [*REPO.glob("scripts/*.py"), *REPO.glob("src/query_pilot/**/*.py")]
    for path in readers:
        if path.name in ("api.py", "live.py", "serve_api.py"):
            continue
        text = path.read_text()
        assert "runs/api" not in text and '"api"' not in text, path


# --- the limits ----------------------------------------------------------------------------------


def test_the_ceilings_admit_every_measured_trajectory() -> None:
    declared = RunConfig.load(api.DECLARATION, [LIVE_TASK_ID])
    assert declared.concurrency == 1
    for name in ("a1-working", "a2-cheap-working"):
        tasks = json.loads((REPO / "results" / f"{name}.json").read_text())["tasks"]
        largest = max(t["prompt_tokens"] + t["completion_tokens"] for t in tasks)
        longest = max(t["elapsed_s"] for t in tasks)
        assert largest < declared.token_ceiling, name
        assert longest < declared.wall_clock_ceiling_s, name
    assert (declared.token_ceiling, declared.wall_clock_ceiling_s) == (30_000, 300)


def test_the_daily_cap_is_the_providers_day_less_what_the_reserve_run_needs() -> None:
    config = ClientConfig.load()
    for role in ("strong", "cheap-no-spillover"):
        endpoint = config.endpoints[config.roles[role].endpoint]
        assert endpoint.limits.tpd == api.PROVIDER_TOKENS_PER_DAY
    a0 = json.loads((REPO / "results" / "a0-working.json").read_text())
    assert a0["tokens"]["total"] == api.RESERVE_RUN_TOKENS == 106_740
    assert api.DAILY_TOKEN_CAP == 93_260
    sizes = [
        json.loads((REPO / "docs" / name).read_text())["attempt_sizes"][
            "projected_attempt_at_ceiling"
        ]
        for name in ("escalation-a1-working.json", "escalation-a2-cheap-working.json")
    ]
    assert api.WORST_ATTEMPT_TOKENS == max(sizes) == 9_777


def _spent(root: Path, run_id: str, model: str, stamps_tokens) -> None:
    directory = root / run_id
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "ledger.jsonl").open("a") as handle:
        for stamp, tokens in stamps_tokens:
            row = {"kind": "attempt", "model": model, "recorded_at": stamp.isoformat(),
                   "prompt_tokens": tokens, "completion_tokens": 0}  # fmt: skip
            handle.write(json.dumps(row) + "\n")


def test_the_daily_cap_admits_at_its_edge_and_refuses_one_token_past_it(tmp_path) -> None:
    clock = FakeClock()
    now = clock.now_utc()
    root = tmp_path / "runs"
    limits = api.Limits(clock, root, reservation=30_000 + api.WORST_ATTEMPT_TOKENS)
    assert limits.reservation == 39_777
    strong, cheap = MODELS["strong"], MODELS["cheap-no-spillover"]
    edge = api.DAILY_TOKEN_CAP - limits.reservation
    assert edge == 53_483

    # Older than a day: does not count. The other model: does not count against this one.
    _spent(root, "20260905-000000-000001", strong, [(now - timedelta(hours=25), 90_000)])
    _spent(root, "20260906-000000-000002", cheap, [(now - timedelta(hours=1), 90_000)])
    recent = [(now - timedelta(hours=23), 3_483), (now - timedelta(hours=2), 50_000)]
    _spent(root, "20260906-000000-000003", strong, recent)
    assert limits.check("127.0.0.1", strong) is None
    _spent(root, "20260906-000000-000004", strong, [(now - timedelta(minutes=5), 1)])
    refusal = limits.check("127.0.0.1", strong)
    assert refusal is not None and refusal.status == 429
    # Refused until the oldest counted spend is a day old: 23 hours ago, so in one hour.
    assert refusal.retry_after_s == pytest.approx(3_600.0)
    assert limits.check("127.0.0.1", cheap) is not None  # 90,000 on the cheap model
    # A restart reads the same spend from the same ledgers.
    assert api.Limits(clock, root, reservation=39_777).check("127.0.0.1", strong) is not None
    clock.advance(3_600.0)
    assert limits.check("127.0.0.1", strong) is None


def test_a_cap_that_no_question_could_fit_under_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="above the daily cap"):
        api.Limits(FakeClock(), tmp_path, reservation=api.DAILY_TOKEN_CAP + 1)


def test_the_per_address_limit_admits_ten_an_hour_and_refuses_the_eleventh(tmp_path) -> None:
    clock = FakeClock()
    limits = api.Limits(clock, tmp_path, reservation=39_777)
    for _ in range(api.PER_IP_QUESTIONS):
        assert limits.check("127.0.0.1", MODELS["strong"]) is None
        limits.admit("127.0.0.1")
        clock.advance(60.0)
    refusal = limits.check("127.0.0.1", MODELS["strong"])
    assert refusal is not None and refusal.status == 429
    assert refusal.retry_after_s == pytest.approx(3_600.0 - 600.0)
    assert limits.check("10.0.0.2", MODELS["strong"]) is None  # another address is unaffected
    clock.advance(3_000.0)
    assert limits.check("127.0.0.1", MODELS["strong"]) is None


def test_the_eleventh_question_in_an_hour_is_refused_by_the_app(tmp_path, database_root) -> None:
    client = Scripted(*[ANSWER] * 11)
    with TestClient(build(tmp_path, database_root, client)) as http:
        for _ in range(api.PER_IP_QUESTIONS):
            reply = ask(http)
            assert reply.status_code == 202
            settle(http, reply.json()["run_id"])
        refused = ask(http)
    assert refused.status_code == 429 and "an hour" in refused.json()["error"]
    assert len(client.handed) == api.PER_IP_QUESTIONS


def test_one_question_runs_at_a_time(tmp_path, database_root) -> None:
    gate = threading.Event()
    client = Scripted(ANSWER, ANSWER, before=held(gate, after=0))
    with TestClient(build(tmp_path, database_root, client)) as http:
        first = ask(http).json()["run_id"]
        second = ask(http)
        assert second.status_code == 429 and second.json()["running"] == first
        assert http.get("/api/status").json()["running"] == first
        gate.set()
        settle(http, first)
        assert ask(http).status_code == 202


def test_the_token_ceiling_stops_a_question_that_crosses_it(tmp_path, database_root) -> None:
    calls = [("", [call("list_tables")])] * 4
    client = Scripted(*calls, ANSWER, tokens=(12_000, 0))
    with TestClient(build(tmp_path, database_root, client)) as http:
        view = settle(http, ask(http).json()["run_id"])
    # 12,000, 24,000, 36,000: the guard sees the ceiling crossed before a fourth request.
    assert len(client.handed) == 3
    assert view["status"] == "failed" and view["incomplete_reason"] == "token_ceiling"


def test_the_token_ceiling_admits_a_question_the_size_of_the_largest_measured(
    tmp_path, database_root
) -> None:
    client = Scripted(("", [call("list_tables")]), ("", [call("list_tables")]), ANSWER,
                      tokens=(9_000, 255))  # fmt: skip
    with TestClient(build(tmp_path, database_root, client)) as http:
        view = settle(http, ask(http).json()["run_id"])
    assert view["footer"]["tokens_in"] + view["footer"]["tokens_out"] == 27_765
    assert view["status"] == "complete"


@pytest.mark.parametrize(("per_request_s", "status"), [(57.6, "complete"), (100.0, "failed")])
def test_the_wall_clock_ceiling_stops_a_stuck_question(
    tmp_path, database_root, per_request_s, status
) -> None:
    clock = FakeClock()

    async def slow(_: int) -> None:
        clock.advance(per_request_s)

    calls = [("", [call("list_tables")])] * 3
    client = Scripted(*calls, ANSWER, before=slow)
    with TestClient(build(tmp_path, database_root, client, clock=clock)) as http:
        view = settle(http, ask(http).json()["run_id"])
    # 57.6 s a request answers on the fourth at 230.4 s; 100 s a request is stopped at 300 s.
    assert view["status"] == status
    if status == "failed":
        assert view["incomplete_reason"] == "wall_clock_ceiling" and len(client.handed) == 3


# --- keys ----------------------------------------------------------------------------------------


def _environment() -> dict[str, str]:
    return {
        "GROQ_API_KEY": SENTINEL,
        "GROQ_API_KEY_2": OTHERS[0],
        "GROQ_API_KEY_3": OTHERS[1],
        "GEMINI_API_KEY": OTHERS[2],
    }


def test_the_api_holds_one_pool_whatever_else_is_set() -> None:
    live = api.live_client(_environment(), http=FakeHttp(), quota_walls=None)
    assert live.pools == ("groq#1",)
    assert live.client.registry.pools("google-ai-studio") == ()
    assert set(live.secrets) == {SENTINEL, *OTHERS}


def _everything_said(http: TestClient, run_id: str, tmp_path: Path, caplog) -> str:
    said = [
        http.get("/api/status").text,
        http.get(f"/api/questions/{run_id}").text,
        http.get("/api/questions/20990101-000000-000000").text,
        ask(http, database="academic").text,
        ask(http, question="").text,
    ]
    return "\n".join(said)


def test_no_key_reaches_a_response_a_log_a_transcript_or_a_ledger(
    tmp_path, database_root, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeHttp(
        replies=[
            groq_reply("", tool_calls=[{"id": "call_1", "type": "function",
                                        "function": {"name": "list_tables", "arguments": "{}"}}],
                       model=MODELS["strong"]),
            groq_reply(ANSWER, model=MODELS["strong"]),
        ]
    )  # fmt: skip
    live = api.live_client(_environment(), http=fake, quota_walls=None, clock=FakeClock())
    app = build(tmp_path, database_root, live.client, secrets=live.secrets, pools=live.pools)
    with TestClient(app) as http:
        run_id = ask(http).json()["run_id"]
        view = settle(http, run_id)
        said = _everything_said(http, run_id, tmp_path, caplog)
    assert view["status"] == "complete"
    # The one key was sent, and only it, and only to pool one.
    assert {r.headers["Authorization"] for r in fake.requests} == {f"Bearer {SENTINEL}"}
    assert {r.pool for r in fake.requests} == {"groq#1"}
    record = [said, caplog.text]
    record += [p.read_text() for p in run_directory(tmp_path, run_id).rglob("*") if p.is_file()]
    for text in record:
        for secret in (SENTINEL, *OTHERS):
            assert secret not in text
        assert not KEYS.search(text), KEYS.search(text)


def test_a_key_a_provider_echoes_back_is_scrubbed_from_every_response_and_log(
    tmp_path, database_root, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    echoed = json.dumps({"error": {"message": f"Invalid API Key: {SENTINEL}",
                                   "code": "invalid_api_key"}})  # fmt: skip
    fake = FakeHttp(replies=[response(echoed, status=401)])
    live = api.live_client(_environment(), http=fake, quota_walls=None, clock=FakeClock())
    app = build(tmp_path, database_root, live.client, secrets=live.secrets, pools=live.pools)
    with TestClient(app) as http:
        run_id = ask(http).json()["run_id"]
        view = settle(http, run_id)
        said = _everything_said(http, run_id, tmp_path, caplog)
    assert view["status"] == "failed" and view["failure"]["error_class"] == "auth"
    assert "[redacted]" in view["failure"]["message"]
    for text in (said, caplog.text):
        assert SENTINEL not in text
    # The run machinery's own record keeps the provider's words (constraint 43); the ledger is
    # local and gitignored, and no provider this project uses echoes a key. Stated, not hidden.


def test_a_fault_in_a_run_is_logged_without_a_key_or_a_traceback(
    tmp_path, database_root, caplog, monkeypatch
) -> None:
    caplog.set_level(logging.DEBUG)

    async def broken(*args, **kwargs):
        raise RuntimeError(f"broken while holding {SENTINEL}")

    monkeypatch.setattr(api.Run, "execute", broken)
    app = build(tmp_path, database_root, Scripted(ANSWER), secrets=(SENTINEL,))
    with TestClient(app) as http:
        ask(http)
        deadline = time.monotonic() + 5
        while "raised RuntimeError" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.01)
    assert "raised RuntimeError" in caplog.text
    assert SENTINEL not in caplog.text and "Traceback" not in caplog.text


# --- the Run button's gate -----------------------------------------------------------------------


def test_the_local_api_answers_as_itself_and_a_static_host_does_not(
    tmp_path, database_root
) -> None:
    with TestClient(build(tmp_path, database_root, Scripted())) as http:
        status = http.get("/api/status")
        page = http.get("/")
    assert status.status_code == 200
    assert status.json()["surface"] == "local API" and status.json()["live"] is True
    assert status.json()["pools"] == ["groq#1"]
    assert re.search(r'<button[^>]*id="run"[^>]*\bdisabled\b', page.text)

    static = FastAPI()
    static.mount("/", StaticFiles(directory=REPO / "viewer", html=True), name="viewer")
    with TestClient(static) as host:
        assert host.get("/api/status").status_code == 404
        assert host.get("/").text == page.text


def test_nothing_under_viewer_could_answer_as_the_api() -> None:
    for path in (REPO / "viewer").rglob("*"):
        assert "api" not in path.relative_to(REPO / "viewer").parts, path


def test_the_page_asks_for_the_api_only_from_a_loopback_address() -> None:
    script = APP_JS.read_text(encoding="utf-8")
    assert 'const LOOPBACK = new Set(["127.0.0.1", "localhost"]);' in script
    assert script.count('fetch("api/status"') == 1
    probe = script[script.index("async function probeLocalApi()") :]
    probe = probe[: probe.index("\n  }\n")]
    assert probe.index("if (!LOOPBACK.has(location.hostname)) return null;") < probe.index(
        'fetch("api/status"'
    )
    assert 'status.surface === "local API" && status.live === true' in probe
    # Run is switched in one place, which does nothing unless the probe found the API.
    assert script.count('$("run").disabled') == 1
    setter = script[script.index("function setRunAvailable") :]
    assert setter.split("\n")[1].strip() == "if (!state.live) return;"
    assert script.count("state.live = ") == 1 and "state.live = status;" in script
    assert script.count("enableLive(") == 2  # its definition, and the probe's one caller
    assert "if (status) {\n      enableLive(status);" in script
    assert script.count('addEventListener("click", submit)') == 1


def test_the_server_binds_loopback_and_nothing_else(monkeypatch) -> None:
    import uvicorn

    seen: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))
    api.serve(FastAPI(), port=9999)
    assert seen["host"] == "127.0.0.1" == api.HOST and seen["port"] == 9999
    assert "host" not in inspect.signature(api.serve).parameters
    script = (REPO / "scripts" / "serve_api.py").read_text()
    assert "--host" not in script and "0.0.0.0" not in script


def test_the_server_serves_nothing_that_loads_from_elsewhere(tmp_path, database_root) -> None:
    with TestClient(build(tmp_path, database_root, Scripted())) as http:
        for path in ("/docs", "/redoc", "/openapi.json", "/../pyproject.toml",
                     "/%2e%2e/pyproject.toml", "/data/../../pyproject.toml"):  # fmt: skip
            assert http.get(path).status_code == 404, path
        assert http.get("/app.js").text == APP_JS.read_text(encoding="utf-8")
        # Served from the working tree, so a browser revalidates rather than run a stale script.
        assert http.get("/app.js").headers["cache-control"] == "no-cache"
        assert http.get("/api/status").headers["cache-control"] == "no-cache"


def test_what_the_api_says_keeps_the_projects_vocabulary(tmp_path, database_root) -> None:
    with TestClient(build(tmp_path, database_root, Scripted(ANSWER))) as http:
        texts = [http.get("/api/status").text, ask(http, database="academic").text]
        run_id = ask(http).json()["run_id"]
        settled = settle(http, run_id)
        settled.pop("system_prompt")  # A0's answer rules, measured with; not the API's words
        texts.append(json.dumps(settled))
        texts.append(ask(http, question="").text)
    texts += [api.MASTHEAD, api.NOTE, api.NOT_SCORED, api.ROWS_RAN]
    for text in texts:
        lowered = text.casefold()
        for word in PROHIBITED:
            assert not re.search(rf"\b{re.escape(word)}\b", lowered), (word, text[:200])
        assert not re.search(r"\barms?\b", lowered)
