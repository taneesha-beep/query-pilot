"""A named, resumable pass over a list of tasks, and the ledger that records it.

**The run ledger is not part of the client.** `query_pilot.client` is general
infrastructure with no knowledge of SQL, no task IDs and no notion of a run, and it stays
that way: this package imports from it and nothing in it imports from here. The seam 1.2
left for exactly this is `QuotaWallSink`, a plain callable — a run hands
:meth:`RunLedger.on_quota_wall` to ``Client.from_config`` and every refusal that names a
quota lands in the run's own ledger, in order beside the attempts around it.

Nothing here knows what a task *is*, either. :class:`Run` executes an injected callable
and every test in this package passes a stub. A0 does not exist until 2.3.
"""

from query_pilot.run.config import (
    DEFAULT_CONCURRENCY,
    RunConfig,
    RunConfigChanged,
    RunError,
    new_run_id,
)
from query_pilot.run.ledger import (
    COMPLETE,
    DEFAULT_RUNS_ROOT,
    ERROR,
    EXECUTOR_ERROR,
    FAILED,
    LEDGER_NAME,
    OK,
    AttemptRow,
    LedgerState,
    RunEndRow,
    RunLedger,
    RunStartRow,
    Segment,
    TaskRow,
    read_ledger,
    read_rows,
)
from query_pilot.run.loop import (
    STATUS_COMPLETE,
    STATUS_INCOMPLETE,
    Run,
    RunReport,
    TaskContext,
    TaskExecutor,
    TaskResult,
)

__all__ = [
    "COMPLETE",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_RUNS_ROOT",
    "ERROR",
    "EXECUTOR_ERROR",
    "FAILED",
    "LEDGER_NAME",
    "OK",
    "STATUS_COMPLETE",
    "STATUS_INCOMPLETE",
    "AttemptRow",
    "LedgerState",
    "Run",
    "RunConfig",
    "RunConfigChanged",
    "RunEndRow",
    "RunError",
    "RunLedger",
    "RunReport",
    "RunStartRow",
    "Segment",
    "TaskContext",
    "TaskExecutor",
    "TaskResult",
    "TaskRow",
    "new_run_id",
    "read_ledger",
    "read_rows",
]
