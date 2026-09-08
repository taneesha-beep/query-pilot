"""What a database looks like, read from the database itself.

**The source is the live schema, not Spider's `tables.json`**, and that is the decision
worth stating. 3.1's tools will discover a schema with `sqlite_master` and `PRAGMA`; if A0
read its schema from a dataset annotation instead, the two agents would be working from
different descriptions of the same world and the A0-against-A1 comparison would be
measuring the descriptions as much as the agents. It is also what the sandbox actually
executes against, so a name that differs between the annotation and the stored schema
becomes a `no such column` rather than a silent divergence.

**What that costs, measured:** `tables.json` carries a readable name per column, and across
the 20 databases the working set touches, 33 of 461 column names differ from the stored
name by more than underscores and case. A few of those carry real information —
`abandoned_yn` is annotated "abandoned yes or no", `uid` is annotated "airline id". A0 does
not see them. Neither will A1, which is what keeps the comparison clean.

Everything here reads through :class:`~query_pilot.sandbox.Sandbox`, so schema
introspection is under the same read-only connection, deadline and caps as everything else,
and the substrate is still never opened directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from query_pilot.sandbox import Sandbox

__all__ = ["Column", "ForeignKey", "Schema", "Table", "read_schema", "render_schema"]

#: Tables SQLite maintains for itself. Not part of anyone's question.
_INTERNAL_PREFIX = "sqlite_"


def quote(identifier: str) -> str:
    """Double-quote an identifier for interpolation, doubling any quote inside it.

    Needed because `PRAGMA table_info(?)` is not a thing — a pragma takes an identifier and
    not a bound parameter — so the name has to be interpolated and therefore has to be
    escaped. Spider's own table names are tame; a Phase 4.2 attack database's will not be.
    """
    return '"' + identifier.replace('"', '""') + '"'


@dataclass(frozen=True, slots=True)
class Column:
    """One column. ``primary_key`` is its **1-based position** within the primary key.

    Not a bool, because `PRAGMA table_info` does not return one: it returns the position,
    and `singer_in_concert` in this substrate has a two-column key. Rendering that as two
    separate inline `PRIMARY KEY` clauses is invalid DDL and tells the model the wrong
    thing about the table — so the position is kept and the renderer emits a composite key
    as the table constraint it actually is.
    """

    name: str
    type: str
    primary_key: int = 0
    not_null: bool = False


@dataclass(frozen=True, slots=True)
class ForeignKey:
    """One column pointing at another. ``to_column`` is None when SQLite left it implicit."""

    column: str
    to_table: str
    to_column: str | None


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    foreign_keys: tuple[ForeignKey, ...] = ()


@dataclass(frozen=True, slots=True)
class Schema:
    db_id: str
    tables: tuple[Table, ...]


def read_schema(sandbox: Sandbox, database: Path | str, db_id: str) -> Schema:
    """Read every table, column, type, primary key and foreign key from one database.

    Raises on failure rather than returning an empty schema. A prompt built from a schema
    that silently came back empty is a task that fails for a reason no ledger row would
    explain, and this is the project's own code reading its own substrate — the one place
    an exception is the honest outcome. The run loop files that as `executor_error`, which
    is exactly what 1.3 reserved the label for.
    """
    listing = sandbox.execute(
        database,
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
    )
    if not listing.ok:
        raise RuntimeError(f"could not read the schema of {db_id}: {listing.error}")

    tables = []
    for (name,) in listing.rows:
        if name.startswith(_INTERNAL_PREFIX):
            continue
        tables.append(_read_table(sandbox, database, db_id, name))
    return Schema(db_id=db_id, tables=tuple(tables))


def _read_table(sandbox: Sandbox, database: Path | str, db_id: str, name: str) -> Table:
    info = sandbox.execute(database, f"PRAGMA table_info({quote(name)})")
    if not info.ok:
        raise RuntimeError(f"could not read {db_id}.{name}: {info.error}")
    columns = tuple(
        Column(
            name=column_name,
            # An untyped column is legal in SQLite and several Spider tables have one. Say
            # so rather than inventing a type the model would then trust.
            type=(column_type or "").strip() or "UNTYPED",
            primary_key=int(primary_key),
            not_null=bool(not_null),
        )
        for _, column_name, column_type, not_null, _, primary_key in info.rows
    )

    keys = sandbox.execute(database, f"PRAGMA foreign_key_list({quote(name)})")
    if not keys.ok:
        raise RuntimeError(f"could not read foreign keys of {db_id}.{name}: {keys.error}")
    foreign_keys = tuple(
        ForeignKey(column=from_column, to_table=to_table, to_column=to_column)
        for _, _, to_table, from_column, to_column, *_ in keys.rows
    )
    return Table(name=name, columns=columns, foreign_keys=foreign_keys)


def render_schema(schema: Schema) -> str:
    """The schema as `CREATE TABLE` statements, which is the form a model has seen most.

    DDL rather than a table of names: it carries types, primary keys and foreign keys in
    one unambiguous shape, and it is what the database itself would say. The largest schema
    the working set touches renders to 3,146 characters, so the whole of every database in
    this substrate fits in one prompt with room to spare — which is what makes a
    single-shot baseline a fair one here rather than a straw man.

    **Row values are deliberately not included.** Sample rows would help a model guess
    literals, and 3.1's tool layer is where that capability belongs; putting them in A0's
    prompt would exceed what 2.3 specifies and would give Phase 4.2's planted row values a
    second, unmeasured way into the prompt.
    """
    blocks = []
    for table in schema.tables:
        lines = [f"CREATE TABLE {quote(table.name)} ("]
        key_columns = sorted(
            (column for column in table.columns if column.primary_key),
            key=lambda column: column.primary_key,
        )
        entries = []
        for column in table.columns:
            entry = f"  {quote(column.name)} {column.type}"
            if column.primary_key and len(key_columns) == 1:
                entry += " PRIMARY KEY"
            elif column.not_null:
                entry += " NOT NULL"
            entries.append(entry)
        if len(key_columns) > 1:
            names = ", ".join(quote(column.name) for column in key_columns)
            entries.append(f"  PRIMARY KEY ({names})")
        for key in table.foreign_keys:
            target = quote(key.to_table)
            if key.to_column is not None:
                target += f" ({quote(key.to_column)})"
            entries.append(f"  FOREIGN KEY ({quote(key.column)}) REFERENCES {target}")
        lines.append(",\n".join(entries))
        lines.append(");")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
