"""Materialise the attack databases from attacks/corpus.json.

Reads the committed corpus and writes one SQLite database per case at
``data/attacks/<db_id>/<db_id>.sqlite`` — the Spider-style layout
:func:`query_pilot.sandbox.database_path` expects, so 4.3 can point a run at
``data/attacks`` the same way it points one at ``data/spider``. The databases are gitignored
and regenerable; `attacks/corpus.json` is the committed source of truth, and the build is
deterministic, so a rebuild is the same database.

Run: uv run python scripts/build_attack_databases.py
"""

from __future__ import annotations

from pathlib import Path

from query_pilot.attacks import build_database, load_corpus
from query_pilot.sandbox import database_path

REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "attacks" / "corpus.json"
ATTACKS_ROOT = REPO / "data" / "attacks"


def main() -> int:
    corpus = load_corpus(CORPUS)
    for case in corpus.cases:
        destination = database_path(ATTACKS_ROOT, case.db_id)
        build_database(case.database, destination)
    print(f"built {len(corpus.cases)} databases under {ATTACKS_ROOT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
