"""Checks on the recorded provider inventory.

These read the committed artifact only. **No test in this project makes a live API call**,
so these cannot tell you a provider works today — only what was observed on the recorded
date, and that the stop-gate was cleared when it was checked.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECK = json.loads((REPO / "docs" / "provider-check.json").read_text())

PROVIDERS = {name: entry for name, entry in CHECK.items() if isinstance(entry, dict)}


def _working_models(provider: str) -> dict[str, dict]:
    return {
        model: probe
        for model, probe in PROVIDERS[provider].get("models", {}).items()
        if probe["completion"]["status"] == 200
    }


def test_at_least_two_providers_answered() -> None:
    # The 0.4 stop-gate. Below two, Phases 1 and 6 lose their point and the project needs
    # re-planning rather than improvisation.
    answering = [name for name in PROVIDERS if _working_models(name)]
    assert len(answering) >= 2, f"only {answering} answered on {CHECK['checked_utc']}"


def test_every_working_model_can_call_a_tool() -> None:
    # Phase 3's agent reaches the database only through tools. A model that answers chat
    # but cannot call a tool is not a candidate, and finding that out here is the point.
    for provider in PROVIDERS:
        for model, probe in _working_models(provider).items():
            assert probe["tool_call"]["tool_calls"], f"{provider}/{model} returned no tool call"


def test_the_nominated_pair_was_verified() -> None:
    for role, provider, model in (
        ("cheap", "groq", "openai/gpt-oss-20b"),
        ("strong", "groq", "openai/gpt-oss-120b"),
    ):
        probe = PROVIDERS[provider]["models"][model]
        assert probe["completion"]["status"] == 200, f"{role} model did not answer"
        assert probe["tool_call"]["tool_calls"], f"{role} model did not call a tool"


def test_no_key_leaked_into_the_report() -> None:
    text = json.dumps(CHECK)
    for marker in ("api_key", "Authorization", "Bearer ", "x-goog-api-key"):
        assert marker not in text, f"{marker!r} appears in the committed provider report"
