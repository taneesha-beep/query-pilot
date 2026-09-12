"""7.2: build the trajectory viewer's data into `viewer/data/`. **Spends nothing.**

Reads committed files only — the projections, the escalation file, the attack results and
corpus, and the lifted transcripts under `tests/transcripts/` — through
`src/query_pilot/viewer.py`, and writes the JSON the static page in `viewer/` reads.

    uv run python scripts/build_viewer.py
    cd viewer && python -m http.server 8000   # then open http://localhost:8000

``tests/test_viewer.py`` rebuilds the data from the same committed inputs and requires the
committed `viewer/data/` back exactly. Nothing here opens a split file or a key.
"""

from __future__ import annotations

import sys
from pathlib import Path

from query_pilot.viewer import build, write

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "viewer" / "data"


def main() -> int:
    files = build(REPO)
    written = write(files, OUT)
    size = sum(path.stat().st_size for path in written)
    print(f"wrote {len(written)} files, {size:,} bytes, under {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
