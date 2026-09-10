"""The prompt-injection corpus: labelled attack cases, and how their databases are built.

Beside `tasks.py` and `splits.py` rather than inside `agents/`, and for the same reason: an
attack case is a fact about a *substrate this project builds*, not about any agent. A0 cannot
be attacked through these at all — it never reads a row value, a column type or a table name
it was not handed — and the corpus is written for A1 and for whatever reaches the tools over
MCP. **This module holds no agent, no client and no run**; it defines what a case is, reads
the committed corpus, and materialises one case's database. Standard library only.

**What is committed and what is generated.** `attacks/corpus.json` is the corpus, committed
as data — every case with its labels *and* the full specification of its database. The
`.sqlite` files under `data/attacks/` are **generated** from that specification by
`scripts/build_attack_databases.py` and are gitignored, exactly as the Spider substrate is.
`build_database` is deterministic — fixed table and row order, explicit primary keys, no
timestamps — so a regenerated database has the same schema and the same rows as the one any
other session built. A test asserts that by dumping two fresh builds and comparing them.

**The label schema is designed against 4.3, not against what is easy to write here.** 4.3
reads three numbers off these labels and each has its own denominator:

- **compliance** — did the trajectory *attempt* the injected instruction — over **all** cases.
  Read mechanically from :attr:`AttackCase.compliance`: either a statement the trajectory ran
  (or answered with) *opens with* one of a set of keywords, or its literal-and-comment-blanked
  text contains one of a set of tokens. Blanking is why a model *quoting* the instruction back
  inside a string is not a false attempt, and why the tokens use ``_`` while the prose of an
  instruction refers to its target with spaces — so a case's own injected text never contains
  its own token and reading the carrier is not mistaken for acting on it.
- **containment** — of the compliant attempts a control *could* stop, how many it did — over
  only the cases where :attr:`AttackCase.containable` is true. Three of the five categories
  have **no** containing control (a `SELECT` that exfiltrates is a legal read; a fixed wrong
  answer and a dodged question execute cleanly), so folding them into a containment rate would
  quote it over the wrong denominator. Those are ``containable = False`` and their
  ``containing_control`` is ``None``.
- **task-damage** — of the cases the agent *resisted*, how many still answered wrongly. That
  needs no field here beyond :attr:`AttackCase.reference_sql`: 4.3 scores the final answer
  against it exactly as A1 is always scored, and crosses that with compliance.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CATEGORIES",
    "CHANNELS",
    "COMPLIANCE_KINDS",
    "CONTAINABLE_CATEGORIES",
    "CONTROLS",
    "PLACEMENTS",
    "AttackCase",
    "Corpus",
    "build_database",
    "dump_content",
    "load_corpus",
]

#: Where the injected instruction hides. The three the roadmap names.
PLACEMENTS: Final = ("names", "metadata", "row_values")

#: The finer surface within a placement. ``names`` splits into a table name and a column name;
#: ``metadata`` is a column's declared type string — SQLite has no column comments, and this is
#: the schema-metadata channel `describe_table` actually surfaces; ``row_values`` is a text
#: value `sample_rows` returns.
CHANNELS: Final = ("table_name", "column_name", "column_type", "row_value")

#: What the injected instruction asks the agent to do.
CATEGORIES: Final = (
    "exfiltrate",
    "destructive",
    "wrong_answer",
    "ignore_question",
    "sandbox_escape",
)

#: The 4.1 controls that can contain an attempt, by their slug in `docs/GUARDRAILS.md`.
CONTROLS: Final = ("ddl_dml_rejection", "multi_statement", "read_only")

#: Which categories a control *can* contain, and which control. The other three categories are
#: absent on purpose: no control fires on a legal read, a clean wrong answer or a dodged
#: question, and 4.3's task-damage rate is what measures those instead.
CONTAINABLE_CATEGORIES: Final = {
    "destructive": "ddl_dml_rejection",
    "sandbox_escape": "ddl_dml_rejection",
}

#: How a compliant attempt is detected in the trajectory's SQL. ``opening_keyword`` — a
#: statement the trajectory ran or answered with opens with one of ``keywords`` (robust
#: against a model merely *reading* a maliciously-named object, which opens with SELECT).
#: ``token`` — the blanked SQL contains one of ``tokens`` as text.
COMPLIANCE_KINDS: Final = ("opening_keyword", "token")


@dataclass(frozen=True, slots=True)
class AttackCase:
    """One attack case: a benign task, an injected instruction, and how to read the outcome.

    The scalar fields are the labels 4.3 reads; ``compliance``, ``surfaces_via`` and
    ``database`` are plain nested data, validated by `tests/test_attacks.py` rather than by a
    type here, because they are read the same way whether they came from this process or from
    the committed file.
    """

    case_id: str
    db_id: str
    placement: str
    channel: str
    category: str
    question: str
    reference_sql: str
    injected_instruction: str
    compliance: Mapping[str, Any]
    containable: bool
    containing_control: str | None
    surfaces_via: Mapping[str, Any]
    database: Mapping[str, Any]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AttackCase:
        return cls(
            case_id=data["case_id"],
            db_id=data["db_id"],
            placement=data["placement"],
            channel=data["channel"],
            category=data["category"],
            question=data["question"],
            reference_sql=data["reference_sql"],
            injected_instruction=data["injected_instruction"],
            compliance=data["compliance"],
            containable=data["containable"],
            containing_control=data["containing_control"],
            surfaces_via=data["surfaces_via"],
            database=data["database"],
        )


@dataclass(frozen=True, slots=True)
class Corpus:
    """The committed corpus: a manifest and its cases."""

    manifest: Mapping[str, Any]
    cases: tuple[AttackCase, ...]

    def __iter__(self):
        return iter(self.cases)

    def __len__(self) -> int:
        return len(self.cases)


def load_corpus(path: Path | str) -> Corpus:
    """Read `attacks/corpus.json`. The one reader; 4.3 uses it too."""
    document = json.loads(Path(path).read_text())
    cases = tuple(AttackCase.from_dict(case) for case in document["cases"])
    manifest = {key: value for key, value in document.items() if key != "cases"}
    return Corpus(manifest=manifest, cases=cases)


def _quote(identifier: str) -> str:
    """Double-quote an identifier, doubling any quote inside it.

    The attack table and column names are hostile by construction — spaces, punctuation, a
    whole sentence — so every name is interpolated through this. It is the same escaping
    `agents.schema.quote` does; kept here so this module depends on nothing in `agents`.
    """
    return '"' + identifier.replace('"', '""') + '"'


def build_database(database: Mapping[str, Any], destination: Path | str) -> None:
    """Materialise one case's database from its specification, deterministically.

    ``database`` is a case's ``database`` mapping: ``{"tables": [{"name", "columns", "rows",
    "primary_key"}]}``. Each column is ``{"name", "type"}`` and the ``type`` is written into
    the ``CREATE TABLE`` **verbatim** — which is what lets the ``metadata`` placement carry an
    instruction as a quoted type string that `describe_table` then surfaces. Tables and rows
    are written in list order, so two builds of one specification are the same database.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    conn = sqlite3.connect(destination)
    try:
        for table in database["tables"]:
            columns: Sequence[Mapping[str, str]] = table["columns"]
            entries = [f"{_quote(column['name'])} {column['type']}" for column in columns]
            primary_key = table.get("primary_key")
            if primary_key:
                entries.append(f"PRIMARY KEY ({', '.join(_quote(name) for name in primary_key)})")
            conn.execute(f"CREATE TABLE {_quote(table['name'])} ({', '.join(entries)})")
            rows = table.get("rows", [])
            if rows:
                placeholders = ", ".join("?" for _ in columns)
                conn.executemany(
                    f"INSERT INTO {_quote(table['name'])} VALUES ({placeholders})",
                    [tuple(row) for row in rows],
                )
        conn.commit()
    finally:
        conn.close()


def dump_content(database: Path | str) -> list[tuple[str, tuple[Any, ...]]]:
    """Every table's schema and rows, in a stable order. What "the same database" means.

    Used by the determinism test rather than a byte comparison of the file: raw SQLite bytes
    can differ across library versions, but the schema and the rows are the database, and they
    do not. Rows are read back in `rowid` order, which is insertion order for these tables.
    """
    conn = sqlite3.connect(f"file:{Path(database)}?mode=ro", uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    try:
        names = [
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if not name.startswith("sqlite_")
        ]
        content: list[tuple[str, tuple[Any, ...]]] = []
        for name in names:
            (sql,) = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
            ).fetchone()
            content.append((f"schema:{name}", (sql,)))
            for row in conn.execute(f"SELECT * FROM {_quote(name)}"):
                content.append((f"row:{name}", tuple(row)))
        return content
    finally:
        conn.close()
