"""5.1's escalation rule, applied to one run directory and written out. **Spends nothing.**

Applies `agents/escalation.py` to every complete trajectory of a run and writes the rule's
table, its definitions and every task's three inputs, beside the request sizes constraint 64 is
about (`agents/attempts.py`). Two committed files come out of it::

    uv run python scripts/escalation_table.py --run-id 20260910-024454-1f69bc \\
        --out docs/escalation-a1-working.json
    uv run python scripts/escalation_table.py \\
        --run-dir tests/transcripts/a2-cheap-smoke-lifted \\
        --out docs/escalation-a2-cheap-smoke.json --without-outcomes

**The first is the rule on 3.6's run — the strong model.** Its transcripts are not committed, so
CI cannot re-read them; `tests/test_escalation.py` checks the per-task inputs this writes against
the two frozen files they must agree with, and recomputes the table from them.

**The second is 5.1's preflight on the cheap model**, read from a byte-for-byte lift that CI does
re-read. ``--without-outcomes`` leaves out whether each task solved: the preflight owes how often
each clause fires, and a per-clause solve rate over fifteen trajectories is what a clause would
be tuned against.

The ceiling derivation the sizes are compared with is read from `docs/a1-tool-sizes.json`, and
the per-minute token budget from the served model's endpoint in `config/providers.toml`, so
neither number is retyped here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from query_pilot.agents import MAX_OUTPUT_TOKENS, PROMPT_CEILING_CHARS
from query_pilot.agents.escalation import document
from query_pilot.client.config import ClientConfig
from query_pilot.run.ledger import LEDGER_NAME, read_rows

REPO = Path(__file__).resolve().parent.parent
TOOL_SIZES = REPO / "docs" / "a1-tool-sizes.json"


def served_tpm(ledger: Path) -> int:
    """The per-minute token budget of the one model that served the run. Refuses on two."""
    models = {
        (row.get("provider"), row.get("model"))
        for row in read_rows(ledger)
        if row.get("kind") == "attempt"
    }
    if len(models) != 1:
        raise SystemExit(
            f"{ledger}: served by {sorted(models)}; one escalation file describes one model"
        )
    provider, model = models.pop()
    config = ClientConfig.load()
    for endpoint in config.endpoints.values():
        if endpoint.provider == provider and endpoint.model == model:
            if endpoint.limits.tpm is None:
                raise SystemExit(f"{provider} {model}: no tpm in config/providers.toml")
            return int(endpoint.limits.tpm)
    raise SystemExit(f"{provider} {model}: not an endpoint in config/providers.toml")


def ceiling_derivation() -> dict:
    budget = json.loads(TOOL_SIZES.read_text())["budget"]
    ratio = budget["chars_per_prompt_token"]
    return {
        "chars_per_prompt_token": ratio,
        "source": "docs/a1-tool-sizes.json, measured in docs/a0-prompt-sizes.json on A0's prompts",
        "worst_case_attempt_tokens": round(PROMPT_CEILING_CHARS / ratio + MAX_OUTPUT_TOKENS),
        "tpm": budget["tpm"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-id", help="read runs/<run-id>/")
    source.add_argument("--run-dir", type=Path, help="read a run directory elsewhere, e.g. a lift")
    parser.add_argument("--out", type=Path, required=True, help="where to write the JSON")
    parser.add_argument(
        "--without-outcomes",
        action="store_true",
        help="leave out whether each task solved (the preflight's form)",
    )
    options = parser.parse_args()

    directory = options.run_dir or REPO / "runs" / options.run_id
    ledger = directory / LEDGER_NAME
    if not ledger.exists():
        print(f"no ledger at {ledger}", file=sys.stderr)
        return 2
    run_id = next((row["run_id"] for row in read_rows(ledger) if row.get("run_id")), None)
    payload = document(
        directory,
        outcomes=not options.without_outcomes,
        tpm=served_tpm(ledger),
        ledger=f"runs/{run_id}/{LEDGER_NAME}",
        ceiling_derivation=ceiling_derivation(),
    )
    options.out.parent.mkdir(parents=True, exist_ok=True)
    options.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("run_id", "terminations", "table")}, indent=2))
    print(json.dumps(payload["attempt_sizes"], indent=2))
    print(f"\nwrote {options.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
