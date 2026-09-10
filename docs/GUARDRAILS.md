# The five controls

Every query this project runs — the model's, the reference's, and the schema reader's —
goes through [`src/query_pilot/sandbox.py`](../src/query_pilot/sandbox.py). Five controls sit
on that path. This document says, for each one, **what it stops and what it does not**,
because a control described only by what it catches is a control whose limits are found the
expensive way.

Two of the five are enforced by the read-only connection and the progress handler, against
any statement that runs. The other two are enforced by `single_read_only_statement` *before*
a connection is opened, and only on the untrusted surface — see
[Where controls 4 and 5 apply](#where-controls-4-and-5-apply) at the end.

The cap and timeout values below are policy choices derived in 2.2 against the largest
legitimate result and the slowest reference query in the 1,034-task frame, recorded in
[`docs/sandbox-caps.json`](sandbox-caps.json) and guarded by tests so they cannot drift
without the derivation being re-run. The two SQLite behaviours cited as measured were
observed on **SQLite 3.53.1** (the version `python -c "import sqlite3; print(sqlite3.sqlite_version)"`
reports on the machine this was built on).

---

## 1. Read-only connection

**Mechanism.** `sqlite3.connect("file:...?mode=ro", uri=True)` opens the database read-only,
and `PRAGMA query_only = ON` is set on top of it. The test for the control is
`test_control_read_only_connection_rejects_a_write`.

**What it stops.** Any statement that writes to a database it has open — `INSERT`, `UPDATE`,
`DELETE`, `DROP`, `CREATE`, `ALTER`, `REINDEX`, and a `WITH ... DELETE`/`INSERT ... SELECT`
that reaches a write — raises `attempt to write a readonly database` and returns an error;
the file on disk is byte-for-byte unchanged.

**Why two mechanisms and not one.** `mode=ro` binds the *main* database and nothing else. A
`mode=ro` connection will `ATTACH` a second file and write into it — measured — and
`PRAGMA query_only = ON` is what refuses that write to an attached database. The negative
control is a committed test (`test_mode_ro_alone_would_let_an_attached_database_be_written`):
if a future SQLite makes the `mode=ro`-only write impossible, that test fails and tells us the
pragma has become belt-and-braces rather than the control it currently is.

**What it does NOT stop.**

- **`ATTACH` itself.** `ATTACH DATABASE '<new file>'` is not a write to an open database; it
  *opens* one, and through a read-only connection it succeeds and **creates a zero-byte file**
  on disk (measured, SQLite 3.53.1). Read-only refuses the write *into* the attached file, not
  the attach. Closing the attach before it runs is **control 4's** job, not this one's.
- **`PRAGMA`.** `PRAGMA writable_schema = ON` and other pragmas return cleanly through a
  read-only connection (measured). They are refused, again, by control 4, not here.
- **Reading data the question did not ask for.** A read-only connection reads *everything*.
  `SELECT * FROM salaries` is a perfectly legal read; if an injected instruction persuades the
  model to run it and reveal the rows, **no control here fires** — nothing illegal executed.
  This is the ceiling on containment and the reason 4.3 also reports a task-damage rate.

## 2. Statement timeout

**Mechanism.** A progress handler reads the wall clock every `PROGRESS_INTERVAL` (10,000) VM
steps and aborts once `STATEMENT_TIMEOUT_S` (**30 s**) has passed. Set on the connection, so
it covers the fetch as well as the first step. Test:
`test_control_statement_timeout_interrupts_a_slow_query`.

**What it stops.** A query that computes without returning — a recursive CTE that never leaves
its first step is the worked case — is interrupted and returns `statement timed out` with
`timed_out=True`, rather than hanging a worker thread for the life of the run. 30 s is 106× the
slowest reference query in the frame (0.2828 s), so nothing this substrate legitimately asks
for is anywhere near it.

**What it does NOT stop.**

- **A fast query that is simply wrong.** The timeout is about termination, not correctness.
- **A correct-but-pathologically-slow reformulation.** A correlated-subquery form returning the
  same rows as a reference was measured at over 60 s against that reference's 0.289 s. No
  timeout value saves that query, so the timeout necessarily costs some correct answers — one
  of the reasons execution accuracy here is a lower bound. What it buys in exchange is a run
  that finishes.
- **A single VM instruction that does not return.** The deadline is sampled between
  instructions, not inside one. Nothing in this substrate does that, and the alternative
  (`Connection.interrupt` from a watchdog thread) has the same limit for the same reason.

## 3. Row and byte caps

**Mechanism.** Rows are kept until `ROW_CAP` (**50,000**) or `BYTE_CAP` (**1,048,576**)
would be exceeded, then the rest are dropped and the result is flagged `truncated`. Both caps
are checked *before* a row is kept, so a truncated result never exceeds the cap it is measured
against. Test: `test_control_caps_truncate_and_flag_a_large_result`.

**What it stops.** A runaway result — a cross join returning 64,000,000 rows is the worked
case — is cut off and comes back marked truncated, so it can neither exhaust memory nor be
mistaken for a complete answer. The two caps bound different pathologies: the row cap bounds
many small rows, the byte cap bounds few very large ones.

**What it does NOT stop.**

- **A small wrong answer.** The caps bound size, not correctness.
- **A result that reaches a cap exactly.** Nothing was lost, so it is not truncated.
- **Deciding a verdict.** Both caps sit 2.4×–3.6× above the largest legitimate reference
  result in the frame, so no reference result is truncated by them, and
  `equivalence.compare` *refuses to compare* a truncated result rather than scoring it wrong.
  A cap that could decide a legitimate answer would be a cap that silently moved the accuracy
  figure; these cannot.

## 4. DDL / DML rejection, before execution

**Mechanism.** `single_read_only_statement` refuses any statement whose one statement does not
open with `SELECT` or `WITH` — a **whitelist**, which is the only defensible direction: a
blocklist of forbidden openings is a list with a hole in it, and the hole is found by the
thing it was guarding against. This runs before a connection is opened. Test:
`test_control_ddl_dml_is_rejected_before_execution`.

**What it stops.** `DROP`, `DELETE`, `UPDATE`, `INSERT`, `ALTER`, `CREATE`, `ATTACH`,
`VACUUM`, `PRAGMA` — everything on Phase 4.1's list — is refused with a stated reason and
**no connection opened**. This is what closes the two holes control 1 leaves: `ATTACH`
creating a file, and `PRAGMA` running. It is also the control that stops a planted destructive
instruction (`DROP TABLE audit`) and a planted sandbox-escape (`ATTACH`, `VACUUM INTO`) at the
untrusted surface — the containable half of the 4.2 corpus.

**Why "before execution" is the point.** Today's read-only connection would refuse a `DROP`
*by executing it and having SQLite raise* — rejection by execution, and only for statements
that happen to be writes. `PRAGMA` and `ATTACH` are not writes and slip that net. A
pre-execution whitelist refuses all of them for one reason, and refuses them without opening
the database at all. The test proves the timing by pointing the guard at a database that does
not exist: a rejected statement returns the guard's reason, where one that reached execution
would fail on the missing file.

**What it does NOT stop.**

- **Exfiltration by a legal read.** `SELECT * FROM secrets` opens with `SELECT` and is
  admitted. Reading a table the user did not ask about is not a write and not illegal SQL, so
  this control — and every control — lets it through. Exfiltration framed as a *read* has no
  containment answer; only exfiltration framed as `INSERT INTO mine SELECT * FROM secrets` is
  caught (it does not open with `SELECT`), and `agents.validate` catches that shape for A1's
  final answer as well.
- **A false rejection of a real read-only construct.** SQLite's bare `VALUES (1), (2)` is
  read-only but does not open with `SELECT`/`WITH`, so the whitelist refuses it. No reference
  in this substrate uses it, and `agents.validate` makes the same choice for the same reason.
- **The model *intending* harm.** This is an execution control, not an intent detector. Whether
  the model *attempted* the injected instruction is 4.3's compliance rate; whether a control
  stopped it is 4.3's containment rate. They are different questions with different
  denominators.

## 5. Multi-statement rejection

**Mechanism.** `single_read_only_statement` counts statements by splitting on `;` at
parenthesis depth zero and outside literals, and refuses more than one — before a connection
is opened. Test: `test_control_multi_statement_is_rejected`.

**What it stops.** A `;`-separated batch — `SELECT 1; DROP TABLE singer` is the worked case —
is refused as one unit with a message naming the one-statement rule, so the second statement
never runs. A `;` inside a string literal (`SELECT 'a;b'`) is a value, not a separator, and is
admitted.

**What it does NOT stop.**

- **Damage inside a single statement.** `DROP TABLE t` is one statement; multi-statement
  rejection has nothing to say about it — that is control 4.
- **Anything, when the caller reached `execute` directly.** SQLite's own driver already raises
  `You can only execute one statement at a time`, so a batch is refused either way. What this
  control adds is a *deterministic, pre-execution* refusal with a clean message, on the
  untrusted surface, for a caller that never reaches SQLite. Its novelty is the timing and the
  message, not the fact of refusal.

---

## Where controls 4 and 5 apply

Controls 4 and 5 run in `Sandbox.execute_guarded`, and `execute_guarded` is reached **only by
the untrusted surface**: A1's `execute_sql` tool and the MCP server, both of which run
arbitrary model-authored SQL. This project's own schema introspection — `PRAGMA table_info`
and `PRAGMA foreign_key_list` in `agents/schema.py`, the row counts in `list_tables` — is
trusted, fixed, project-authored SQL, and it stays on the unguarded `Sandbox.execute`. If a
blanket "reject PRAGMA" applied everywhere, it would refuse the schema reader's own pragmas
and the tools could not describe a table.

The split is deliberate and it is a layering statement:

- The **rule** (`single_read_only_statement`) lives in `sandbox.py`. It is standard-library
  only and knows nothing of `agents` or `equivalence`, so it does not invert the dependency
  the rest of the module keeps — `agents` imports `sandbox`, never the reverse.
- The **decision to apply it** — which caller is trusted — lives in `agents/tools.py`, the one
  layer that knows both how a statement is executed and whether it came from the model.

This is the second of the two layers Phase 4.1 tests. `agents/validate.py` refuses A1's
*final answer* (with a repair the model can act on); the sandbox refuses *every* statement the
tool surface runs. A control at the sandbox is not a duplicate of the one at the validator —
it is the one that holds when the caller is not A1: an MCP client, the reference path's
neighbour, or any future agent.

## What no control can contain

Three of the five instruction categories in the 4.2 corpus have **no containing control**, and
this is stated here rather than discovered in 4.3:

| Category | Contained by | Why |
|---|---|---|
| Perform a destructive operation | Control 4 | `DROP`/`DELETE`/… do not open with `SELECT`/`WITH`. |
| Escape the sandbox path | Control 4 | `ATTACH`/`VACUUM INTO`/`PRAGMA` do not either. |
| Exfiltrate another table | *nothing* | A `SELECT` read of another table is legal SQL. |
| Return a fixed wrong answer | *nothing* | The final query executes cleanly; it is simply wrong. |
| Ignore the user's question | *nothing* | Answering a different question executes cleanly too. |

For the bottom three, the only measurable harm is that the answer is wrong, which is why 4.3
reports compliance (did the model attempt it) and a task-damage rate (did resisting it still
break the answer) alongside containment — and why 4.2 labels every case with whether a control
*can* fire, so the containment rate is quoted over the right denominator and not over all
cases.
