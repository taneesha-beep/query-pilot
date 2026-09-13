"""7.4: every figure the README leads with is a committed summary's, in the project's words.

The README is short on purpose and links to the results page and `docs/` for everything else. What
it does show is checked here against the committed files it came from, so a figure cannot drift
from its summary without CI saying so.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
README = (REPO / "README.md").read_text(encoding="utf-8")
SITE = "https://taneesha-beep.github.io/query-pilot/"
PROHIBITED = (
    "ladder", "rung", "harness", "judge", "golden set", "ndcg", "relevance", "held-out",
    "bootstrap", "ablation", "attribution", "configuration", "residual", "hardened",
    "<pending>", "[measured]", "correct", "incorrect", "lower bound",
)  # fmt: skip


def load(relative: str) -> dict:
    return json.loads((REPO / relative).read_text(encoding="utf-8"))


def share(block: dict, sep: str = " / ", count: str = "solved") -> str:
    return f"{block[count]}{sep}{block['of']} — {block['percent']}%"


def test_the_readme_leads_with_the_comparison_then_containment_then_the_cost_frontier() -> None:
    headings = re.findall(r"^#{2,3} (.+)$", README, flags=re.MULTILINE)
    results = headings[headings.index("Results") + 1 :]
    assert results[:4] == ["A0 against A1", "Containment", "Cost frontier", "Reserve set"]


def test_every_figure_it_shows_is_a_committed_summarys() -> None:
    a0, a1 = load("results/a0-working.json"), load("results/a1-working.json")
    cheap, a2 = load("results/a2-cheap-working.json"), load("results/a2-working.json")
    attacks = load("results/attacks.json")
    for document in (a0, a1, cheap, a2):
        assert share(document["execution_accuracy"]) in README
    for document in (a0, a1):
        assert f"{document['tokens']['total']:,}" in README
    for document in (a0, a1, cheap, a2):
        assert f"{document['tokens']['per_solved_task']:,}" in README
    for key in ("compliance", "containment", "task_damage"):
        assert share(attacks[key], " of ", "count") in README
    ratio = round(a1["tokens"]["total"] / a0["tokens"]["total"], 2)
    assert (
        f"{a0['execution_accuracy']['solved'] - a1['execution_accuracy']['solved']} fewer" in README
    )
    assert f"{ratio}\u00d7 the tokens" in README
    assert f"{round(a2['frontier']['break_even_price_ratio'] * 100, 2)}%" in README
    assert a2["frontier"]["verdict"]["cascade_wins"] is False and "cascade lost" in README


def test_the_reserve_figure_sits_beside_the_working_figure() -> None:
    """Changed on purpose after the reserve run: `**TBD.**` gave way to the figure.

    Beside the working figure, not in a footnote, with its difference and no adjective, and the
    limitation `docs/SPLITS.md` says is repeated wherever the figure is reported.
    """
    reserve = load("results/a0-reserve.json")
    beside = reserve["beside_the_working_set"]
    section = README[README.index("### Reserve set") : README.index("## Try it")]
    assert "TBD" not in section
    assert beside["working"]["run_id"] == load("results/a0-working.json")["measurement"]["run_id"]
    rows = [line for line in section.splitlines() if line.startswith("| ") and " set " in line]
    assert share(beside["working"]) in rows[0] and "Working set" in rows[0]
    assert f"**{share(beside['reserve'])}**" in rows[1] and "Reserve set" in rows[1]
    for row, side in zip(rows, ("working", "reserve"), strict=True):
        floor = beside["empty_result_floor"][side]
        assert f"{floor['tasks']} of {floor['of']}" in row
    difference = beside["difference"]
    assert difference["tasks"] < 0
    assert f"**{-difference['tasks']} fewer matches on the reserve set" in section
    assert f"\N{MINUS SIGN}{-difference['percentage_points']} percentage points" in section
    assert "new questions, not new schemas" in section
    assert "no measured spread" in section


def test_every_measured_table_names_its_provider_model_date_and_ledger() -> None:
    for document in (
        load("results/a0-working.json"),
        load("results/a1-working.json"),
        load("results/a2-cheap-working.json"),
        load("results/attacks.json"),
        load("results/a0-reserve.json"),
    ):
        measurement = document["measurement"]
        assert f"`runs/{measurement['run_id']}/ledger.jsonl`" in README
        assert measurement["date"] in README
        assert f"`{measurement['provider_and_model'][0].split('/', 1)[1]}`" in README


def test_it_links_the_deployed_page_and_the_results_page() -> None:
    assert f"({SITE})" in README
    assert f"({SITE}results.html)" in README
    assert "Reserve set" in README


def test_it_keeps_the_projects_vocabulary() -> None:
    lowered = README.casefold()
    assert "matches the reference" in lowered and "not proof of a right answer" in lowered
    for word in PROHIBITED:
        assert not re.search(rf"\b{re.escape(word)}\b", lowered), word
    assert not re.search(r"\barms?\b", lowered)
    # The derived queue wait is never quoted without its caveat (constraint 102).
    assert "queue" not in lowered


def test_every_local_link_resolves() -> None:
    for target in re.findall(r"\]\((?!https?://)([^)#]+)\)", README):
        assert (REPO / target).exists(), target
