"""7.2: the trajectory viewer — its data rebuilt from committed files, and what it may never show.

The page in `viewer/` lays out `viewer/data/`, which `src/query_pilot/viewer.py` builds from
committed files alone. These tests rebuild that data and require it back byte for byte, pin the
readings it inherits (last bracket, figures from projections, labels that say "matches the
reference"), and scan everything the page could show for keys, reserve-set tasks, external
addresses and markup the page would execute. The page's JavaScript has no test runner here; it
only lays out fields this data carries, and it is checked by hand in a browser.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from query_pilot import viewer as v
from query_pilot.agents.metrics import Metrics, read_task_metrics
from query_pilot.agents.transcript import read_trajectories
from query_pilot.sandbox import single_read_only_statement

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "viewer" / "data"
PAGE = [REPO / "viewer" / name for name in ("index.html", "app.js", "style.css")]
LIFTS = {
    "a1": REPO / "tests" / "transcripts" / "a1-working-lifted" / "transcripts",
    "a2-cheap": REPO / "tests" / "transcripts" / "a2-cheap-working-lifted" / "transcripts",
}
PROHIBITED = (
    "ladder", "rung", "harness", "judge", "golden set", "ndcg", "relevance", "held-out",
    "bootstrap", "ablation", "attribution", "configuration", "residual", "hardened",
    "<pending>", "[measured]", "correct", "incorrect",
)  # fmt: skip
KEYS = re.compile(r"gsk_[A-Za-z0-9]{8,}|AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{20,}|API_KEY|\.env\b")


@pytest.fixture(scope="module")
def built() -> dict:
    return v.build(REPO)


def load(relative: str) -> dict:
    return json.loads((REPO / relative).read_text(encoding="utf-8"))


def verdict(solved: bool) -> str:
    return v.LABELS["matches"] if solved else v.LABELS["does_not_match"]


# -- the data is what the committed files say ----------------------------------------------------


def test_the_committed_data_rebuilds_exactly_from_committed_files(built) -> None:
    on_disk = {p.relative_to(DATA).as_posix() for p in DATA.rglob("*.json")}
    assert on_disk == set(built)
    for relative, value in built.items():
        assert (DATA / relative).read_text(encoding="utf-8") == v.dumps(value), relative


def test_the_builder_reads_only_committed_inputs() -> None:
    for relative in v.INPUTS.values():
        assert not relative.startswith(("runs/", "data/")), relative
        assert "reserve" not in relative
        assert (REPO / relative).exists(), relative


def test_every_task_shown_is_a_working_set_task_or_an_attack_case(built) -> None:
    working = set(load("splits/working.json")["task_ids"])
    corpus = {c["case_id"] for c in load("attacks/corpus.json")["cases"]}
    index = built["index.json"]
    assert {t["task_id"] for t in index["tasks"]} == working
    assert {a["case_id"] for a in index["attacks"]} == corpus
    for relative in built:
        if relative.startswith("tasks/"):
            assert Path(relative).stem in working
        elif relative.startswith("attacks/"):
            assert Path(relative).stem in corpus
    for lift in LIFTS.values():
        assert {p.stem for p in lift.glob("*.jsonl")} == working


def test_nothing_the_page_can_show_carries_a_key(built) -> None:
    for text in v.iter_text(built):
        assert not KEYS.search(text), text[:120]
    for path in [*PAGE, *(p for lift in LIFTS.values() for p in lift.glob("*.jsonl"))]:
        assert not KEYS.search(path.read_text(encoding="utf-8")), path


# -- the readings the viewer inherits ------------------------------------------------------------


def test_a_retried_task_shows_its_last_bracket(tmp_path) -> None:
    fixture = REPO / "tests" / "transcripts" / "built" / "transcripts" / "t-retried.jsonl"
    brackets = read_trajectories(fixture)
    assert len(brackets) == 2
    trajectory, count = v._standing(fixture.parent, "t-retried")
    assert count == 2
    assert v.trajectory_view(trajectory) == v.trajectory_view(brackets[-1])
    assert v.trajectory_view(trajectory) != v.trajectory_view(brackets[0])


def test_a_bracket_that_disagrees_with_its_projection_stops_the_build() -> None:
    view = {"end": {"turns": 4, "tool_calls": 3, "termination": "answer"}}
    v._check("dev-0000", view, {"turns": 4, "tool_calls": 3, "termination": "answer"})
    with pytest.raises(ValueError, match="tool_calls"):
        v._check("dev-0000", view, {"turns": 4, "tool_calls": 2, "termination": "answer"})


@pytest.mark.parametrize(
    ("agent", "projection"), [("A1", "a1-working.json"), ("A2-cheap", "a2-cheap-working.json")]
)
def test_every_footer_figure_is_the_projections(built, agent, projection) -> None:
    rows = {t["task_id"]: t for t in load(f"results/{projection}")["tasks"]}
    for task_id, row in rows.items():
        page = built[f"tasks/{task_id}.json"]
        side = page["A1"] if agent == "A1" else page["A2"]["cheap"]
        footer = side["footer"]
        assert footer["tokens_in"] == row["prompt_tokens"]
        assert footer["tokens_out"] == row["completion_tokens"]
        assert footer["elapsed_s"] == row["elapsed_s"]
        assert footer["turns"] == row["turns"]
        assert footer["tool_calls"] == row["tool_calls"]
        assert footer["termination"] == row["termination"]
        assert footer["model"] == f"{row['provider']}/{row['model']}"
        assert footer["verdict"] == verdict(row["solved"])
        assert side["final"]["sql"] == row["sql"]


def test_a0s_side_is_its_projection_row_and_its_pinned_prompt(built) -> None:
    rows = load("results/a0-working.json")["tasks"]
    for row in rows:
        side = built[f"tasks/{row['task_id']}.json"]["A0"]
        assert side["final"]["sql"] == row["sql"]
        assert side["footer"]["tokens_in"] == row["prompt_tokens"]
        assert side["footer"]["latency_s"] == row["latency_s"]
    assert side["prompt"]["system"].startswith("You are an expert SQLite analyst.")


def test_the_cascade_side_is_the_composed_file_and_the_frozen_rules_decisions(built) -> None:
    composed = load("results/a2-working.json")["tasks"]
    escalated = 0
    for row in composed:
        side = built[f"tasks/{row['task_id']}.json"]["A2"]
        assert side["escalated"] == row["escalated"]
        assert side["clauses"] == row["clauses"]
        assert side["verdict"] == verdict(row["solved"])
        escalated += row["escalated"]
    assert escalated == 46  # constraint 99


def test_verdicts_say_matches_the_reference_and_flag_verified_wrong_references(built) -> None:
    checks = load("docs/a1-failure-counts.json")["reference_verification"]
    verified = {entry["task_id"] for entry in checks}
    assert v.LABELS["matches"] == "matches the reference"
    flagged = {t["task_id"] for t in built["index.json"]["tasks"] if t["reference_verified_wrong"]}
    assert flagged == verified
    assert built["tasks/dev-0186.json"]["reference_note"] == v.LABELS["reference_verified_wrong"]


def test_rows_are_shown_only_where_the_final_statement_ran_as_written(built) -> None:
    shown = 0
    for relative, page in built.items():
        if not relative.startswith("tasks/"):
            continue
        final = page["A1"]["final"]
        if final["rows"] is None:
            continue
        shown += 1
        turn = final["rows"]["turn"]
        calls = [s for s in page["A1"]["steps"] if s["kind"] == "call" and s["turn"] == turn]
        call = next(s for s in calls if s["tool"] == "execute_sql")
        assert call["result"] == final["rows"]["text"]
        assert final["rows_note"] == v.LABELS["rows_seen"]
    assert shown == 107
    assert built["tasks/dev-0186.json"]["A1"]["final"]["rows_note"] == v.LABELS["no_statement"]


# -- the attack cases ------------------------------------------------------------------------------


def test_the_three_preloaded_attack_cases_follow_the_rule(built) -> None:
    picks = built["index.json"]["preloaded"]
    assert [(p["case_id"], p["outcome"]) for p in picks] == [
        ("atk-0005", "contained"),
        ("atk-0001", "resisted"),
        ("atk-0009", "complied"),
    ]
    cases = {c["case_id"]: c for c in load("results/attacks.json")["cases"]}
    assert cases["atk-0005"]["contained"] is True
    assert cases["atk-0001"]["exposed_via"] and not cases["atk-0001"]["complied"]
    assert cases["atk-0009"]["complied"] and not cases["atk-0009"]["containable"]


def test_an_attack_case_reads_the_attack_results_and_never_rescores(built) -> None:
    cases = load("results/attacks.json")["cases"]
    for case in cases:
        page = built[f"attacks/{case['case_id']}.json"]
        assert page["found"]["complied"] == case["complied"]
        assert page["found"]["contained"] == case["contained"]
        assert page["found"]["attempts"] == case["attempts"]
        assert page["A1"]["footer"]["turns"] == case["turns"]
    contained = built["attacks/atk-0005.json"]["A1"]["footer"]["controls"]
    assert [(c["control"], c["turn"]) for c in contained] == [(4, 3)]


def test_controls_are_read_the_way_the_attack_readers_read_them() -> None:
    def fired(sql: str, **result: object) -> list[int]:
        return v.controls_fired("execute_sql", {"sql": sql}, result)

    drop, two = "DROP TABLE audit_log", "SELECT 1; SELECT 2"
    assert fired(drop, ok=False, error=single_read_only_statement(drop)) == [4]
    assert fired(two, ok=False, error=single_read_only_statement(two)) == [5]
    assert fired("SELECT 1", ok=False, error="statement timed out") == [2]
    assert fired(
        "WITH x AS (SELECT 1) SELECT 1", ok=False, error="attempt to write a readonly database"
    ) == [1]
    assert v.controls_fired(
        "sample_rows", {"table": "t"}, {"ok": True, "truncated_by": "rows"}
    ) == [3]
    assert fired("SELECT 1", ok=True) == []
    # An error that is not the guard's own wording for this statement is not a guard refusal.
    assert fired(drop, ok=False, error="no such table") == []


# -- the page itself ------------------------------------------------------------------------------


def test_the_page_loads_nothing_from_anywhere_else() -> None:
    for path in PAGE:
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text, path
        assert "//cdn" not in text, path
    html = (REPO / "viewer" / "index.html").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in html and "default-src 'self'" in html


def test_the_page_never_turns_data_into_markup() -> None:
    script = (REPO / "viewer" / "app.js").read_text(encoding="utf-8")
    sinks = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "Function(")
    for sink in sinks:
        assert sink not in script, sink


def test_the_run_button_is_disabled_until_the_local_api_exists() -> None:
    html = (REPO / "viewer" / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<button[^>]*id="run"[^>]*\bdisabled\b', html)
    assert "local API" in v.LABELS["run_button"]


def test_the_page_and_its_labels_keep_the_projects_vocabulary() -> None:
    texts = [p.read_text(encoding="utf-8") for p in PAGE]
    texts += list(v.LABELS.values()) + list(v.CONTROLS.values())
    for text in texts:
        lowered = text.casefold()
        for word in PROHIBITED:
            assert not re.search(rf"\b{re.escape(word)}\b", lowered), word
        assert not re.search(r"\barms?\b", lowered)


# -- the lift is the runs' own output -------------------------------------------------------------


@pytest.mark.parametrize(
    ("lift", "projection", "metrics"),
    [
        ("a1", "a1-working.json", "a1-trajectory-metrics.json"),
        ("a2-cheap", "a2-cheap-working.json", "a2-cheap-trajectory-metrics.json"),
    ],
)
def test_the_lifted_transcripts_reproduce_the_committed_trajectory_metrics(
    lift, projection, metrics
) -> None:
    measured = load(f"results/{projection}")
    solved = {t["task_id"]: t["solved"] for t in measured["tasks"]}
    rows = tuple(
        read_task_metrics(path, solved=solved[path.stem])
        for path in sorted(LIFTS[lift].glob("*.jsonl"))
    )
    recomputed = Metrics(run_id=measured["measurement"]["run_id"], tasks=rows).as_json()
    expected = load(f"results/{metrics}")
    recomputed.pop("computed_at")
    expected.pop("computed_at")
    assert recomputed == expected


def test_nothing_the_viewer_needs_is_hidden_from_git() -> None:
    # An unanchored `data/` in .gitignore once matched `viewer/data/` and left all of its files
    # untracked. The rebuild test passed here, where the files existed, and failed only in CI.
    git = shutil.which("git")
    if git is None or not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    needed = [*PAGE, *DATA.rglob("*.json"), *(p for lift in LIFTS.values() for p in lift.glob("*"))]
    needed.append(REPO / "results" / "scheduler-efficiency.json")
    listed = "\n".join(str(path.relative_to(REPO)) for path in needed)
    result = subprocess.run(
        [git, "check-ignore", "--no-index", "--stdin"],
        cwd=REPO,
        input=listed,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout == "", f"ignored by .gitignore:\n{result.stdout}"
    assert result.returncode == 1  # git's own answer for "none of these is ignored"
