"""Serve the viewer with its Run button alive, on 127.0.0.1 only. **A question spends real quota.**

7.1's local API (`src/query_pilot/api.py`): the page in `viewer/`, served from this machine,
plus ``/api/*``, which runs a typed question as A1 or A2-cheap and records it under
`runs/api/<run_id>/`. The deployed page is the same files with no API behind them, so its Run
button stays disabled (constraint 79).

**What a question costs.** On A1's measured run (Groq, `openai/gpt-oss-120b`, 2026-09-10, run
`20260910-024454-1f69bc`) a task averaged 5.51 requests and 5,140.2 tokens; at most 15 requests,
by the loop's own limits. The API holds `GROQ_API_KEY` alone — pool `groq#1` — and stops
admitting questions on a model once its last 24 hours reach the daily cap in `api.py`.

**The keys are not loaded for you**, as with every script that spends quota (constraint 45)::

    set -a && . ./.env && set +a
    uv run python scripts/serve_api.py [--port 8765]

Then open http://127.0.0.1:8765/ in a browser.
"""

from __future__ import annotations

import argparse
import logging
import sys

from query_pilot.agents.live import AGENTS
from query_pilot.api import DATABASE_ROOT, DATABASES, HOST, create_app, live_client, serve
from query_pilot.client import ClientError
from query_pilot.sandbox import database_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="the loopback port to listen on")
    options = parser.parse_args()

    missing = [db for db in DATABASES if not database_path(DATABASE_ROOT, db).exists()]
    if missing:
        print(f"substrate not acquired ({len(missing)} of {len(DATABASES)} databases missing); "
              "see docs/SUBSTRATE.md", file=sys.stderr)  # fmt: skip
        return 2

    live = live_client()
    # Preflight, for the reason every spending script gives: a missing key is one fact about the
    # environment, and finding it once here beats every question discovering it separately.
    try:
        for spec in AGENTS.values():
            live.client.registry.candidates(spec.role)
    except ClientError as exc:
        print(f"{exc}\n\nLoad the keys first:  set -a && . ./.env && set +a", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # httpx logs every provider request at INFO; the API's own line per question is enough.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app(client=live.client, secrets=live.secrets, pools=live.pools, owns_client=True)
    print(f"serving on http://{HOST}:{options.port}/ — pools {', '.join(live.pools)}")
    serve(app, port=options.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
