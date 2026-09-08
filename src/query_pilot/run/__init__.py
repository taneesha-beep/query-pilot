"""A named, resumable pass over a list of tasks, and the ledger that records it.

**The run ledger is not part of the client.** `query_pilot.client` is general
infrastructure with no knowledge of SQL, no task IDs and no notion of a run, and it stays
that way: this package imports from it and nothing in it imports from here. The seam 1.2
left for exactly this is `QuotaWallSink`, a plain callable — a run hands
:meth:`RunLedger.on_quota_wall` to ``Client.from_config`` and every refusal that names a
quota lands in the run's own ledger, in order beside the attempts around it.

Nothing here knows what a task *is*, either. :class:`Run` executes an injected callable
and every test in this package passes a stub. A0 does not exist until 2.3.

Three things this package owns and the client deliberately does not: an append-only ledger
a killed run resumes from, a budget guard that stops a run at a declared ceiling rather
than letting it degrade, and a summary that refuses to present an unfinished run as a
result.
"""

from query_pilot.run.config import (
    DEFAULT_CONCURRENCY,
    RunConfig,
    RunConfigChanged,
    RunError,
    new_run_id,
)
from query_pilot.run.guard import (
    FATAL_ERROR_CLASSES,
    BudgetGuard,
    IncompleteReason,
    fatal_reason,
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
from query_pilot.run.summary import (
    SUMMARY_NAME,
    IncompleteRun,
    Summary,
    read_summary,
    summarise,
    write_summary,
)

__all__ = [
    "COMPLETE",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_RUNS_ROOT",
    "ERROR",
    "EXECUTOR_ERROR",
    "FAILED",
    "FATAL_ERROR_CLASSES",
    "LEDGER_NAME",
    "OK",
    "STATUS_COMPLETE",
    "STATUS_INCOMPLETE",
    "SUMMARY_NAME",
    "AttemptRow",
    "BudgetGuard",
    "IncompleteReason",
    "IncompleteRun",
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
    "Summary",
    "TaskContext",
    "TaskExecutor",
    "TaskResult",
    "TaskRow",
    "fatal_reason",
    "new_run_id",
    "read_ledger",
    "read_rows",
    "read_summary",
    "summarise",
    "write_summary",
]
