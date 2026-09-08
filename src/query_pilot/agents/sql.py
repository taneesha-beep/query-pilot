"""Getting one SQL statement out of whatever a model actually said.

This is small and it decides a real share of the number, which is why it is its own module
with its own tests rather than a regex inside the agent. A baseline that fails to find the
SQL a model correctly wrote is a baseline that manufactures a result in A1's favour, and
that is the specific failure 2.3 is warned about.

Four shapes have to work, because models produce all four:

1. a bare statement;
2. a statement in a ```` ```sql ```` fence;
3. a statement in a bare ```` ``` ```` fence;
4. a statement buried in prose — "Here is the query: SELECT ..." — with or without a
   trailing explanation.

**The last fenced block wins.** A model that shows its working and then commits to an
answer puts the answer last, and a model that gives one block gives the same block either
way. Taking the first would pick a draft over a correction.

**The first statement of the chosen block wins**, and any statement after it is counted and
dropped. 2.3 owes "output parsed for a single SQL statement"; *rejecting* multi-statement
input is Phase 4.1's control and a different layer, so the count is recorded rather than
turned into a refusal here.

This has its own scanner rather than sharing one with `equivalence.orders_rows` or
`sandbox.has_statement`. The three answer different questions — where the parentheses are,
whether an ORDER BY is outermost, whether there is anything to run — and wiring the prompt
parser into the measurement instrument to save fifteen lines would be the worse trade.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from query_pilot.sandbox import has_statement

__all__ = ["NO_SQL_FOUND", "Extraction", "extract_sql", "split_statements", "strip_fences"]

#: Why nothing came back. Carried into the task's `detail` so 2.5 can read it.
NO_SQL_FOUND = "the response contained no SQL statement"

#: A fenced code block, with an optional language tag. Non-greedy so consecutive blocks stay
#: separate, and DOTALL so a block spans lines.
_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)

#: Where a statement can begin, in unfenced prose.
#:
#: `WITH` matters, because a common-table-expression query is a normal answer and starts
#: with neither SELECT nor a fence. But **`\bWITH\b` alone matches the English word** — "I
#: can't help with that" was read as the start of a CTE, and the model's refusal became a
#: syntax error instead of the "no SQL" it was. So `WITH` must be followed by a name and
#: `AS (`, which is what a CTE actually looks like and what prose never does.
#:
#: `SELECT` is left as a bare word. Prose using it as a verb — "Select the names" — would
#: be extracted and would fail in the sandbox as a syntax error, which is a `candidate_error`
#: rather than a false solve. That is the safe direction, and tightening it further would
#: start rejecting real answers.
_OPENING = re.compile(
    r"\bSELECT\b|\bWITH\s+(?:RECURSIVE\s+)?[\w\"'`\[][^\s(]*\s+AS\s*\(",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Extraction:
    """One statement, or a stated reason there is none.

    ``dropped`` counts statements found after the first and thrown away, so 2.5 can tell a
    model that answered twice from one that answered once.
    """

    sql: str | None
    reason: str | None = None
    dropped: int = 0
    fenced: bool = False

    @property
    def found(self) -> bool:
        return self.sql is not None


def strip_fences(text: str) -> tuple[str, bool]:
    """The content of the last fenced block that holds a statement, or the text unchanged.

    A block whose language tag names something other than SQL is still considered: models
    label SQL as ``sqlite``, ``postgres`` or nothing at all, and a tag is a weaker signal
    than the content. Blocks holding no statement at all — an empty fence, a fence of prose
    with no SELECT in it — are skipped rather than being allowed to shadow a real one.
    """
    blocks = [block for _, block in _FENCE.findall(text)]
    for block in reversed(blocks):
        if has_statement(block) and _OPENING.search(_blank(block)):
            return block, True
    return text, False


def split_statements(sql: str) -> list[str]:
    """Split on `;` at parenthesis depth zero, ignoring semicolons inside literals.

    Depth matters less than the literals do — a `;` inside `'a;b'` is a value, not a
    separator — but both are cheap to track in one pass and getting either wrong truncates
    a valid query into an invalid one.
    """
    blanked = _blank(sql)
    statements = []
    depth = 0
    start = 0
    for index, char in enumerate(blanked):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == ";" and depth <= 0:
            statements.append(sql[start:index])
            start = index + 1
    statements.append(sql[start:])
    return [statement for statement in statements if has_statement(statement)]


def extract_sql(text: str) -> Extraction:
    """The single statement this response is offering, if it is offering one."""
    if not text or not text.strip():
        return Extraction(None, reason=NO_SQL_FOUND)

    region, fenced = strip_fences(text)
    if not fenced:
        # Unfenced, so the statement is somewhere in prose. Start it at the first SELECT or
        # WITH and let the statement splitter end it: anything before that word is a
        # preamble, and a preamble left attached is a syntax error rather than an answer.
        opening = _OPENING.search(_blank(region))
        if opening is None:
            return Extraction(None, reason=NO_SQL_FOUND)
        region = region[opening.start() :]

    statements = split_statements(region)
    if not statements:
        return Extraction(None, reason=NO_SQL_FOUND, fenced=fenced)

    first = statements[0].strip()
    if not fenced:
        # An unfenced statement with no terminating semicolon runs to the end of the
        # response, which may be prose the model added after its answer. Nothing can
        # reliably tell "ORDER BY name" from "This orders by name", so the whole tail is
        # kept and the sandbox reports the syntax error. Making that a solve is not
        # available; making it silent would be worse.
        first = first.rstrip()
    return Extraction(first, dropped=len(statements) - 1, fenced=fenced)


def _blank(sql: str) -> str:
    """Replace literals, quoted identifiers and comments with spaces, keeping length.

    Length is preserved so an offset into the blanked text indexes the original. This is a
    lexer and not a parser: it cannot be fooled by a `;`, a `(` or the word `SELECT` inside
    a string or a comment, and it does not understand anything else.
    """
    out = list(sql)
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char in "'\"`[":
            closing = {"'": "'", '"': '"', "`": "`", "[": "]"}[char]
            end = index + 1
            while end < length:
                if sql[end] == closing:
                    # A doubled quote is an escaped quote and not the end of the literal.
                    if closing != "]" and end + 1 < length and sql[end + 1] == closing:
                        end += 2
                        continue
                    break
                end += 1
            for position in range(index, min(end + 1, length)):
                out[position] = " "
            index = end + 1
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end == -1 else end
            for position in range(index, end):
                out[position] = " "
            index = end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = length if end == -1 else end + 2
            for position in range(index, end):
                out[position] = " "
            index = end
        else:
            index += 1
    return "".join(out)
