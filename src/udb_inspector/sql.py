"""Reading the SQL the user typed: statements, read-only checks, syntax errors."""

from __future__ import annotations

import logging
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import Token, TokenType

# sqlglot logs a warning for every statement it can only parse loosely.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

SQLGLOT_DIALECTS = {
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "sqlite": "sqlite",
    "mssql": "tsql",
    "oracle": "oracle",
    "duckdb": "duckdb",
}
# On these, DDL commits on its own, so rolling back can't undo it and a
# statement we can't classify must not run.
IMPLICIT_COMMIT = {"mysql", "oracle"}

WRITES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.TruncateTable)
READ_COMMANDS = {"EXPLAIN", "SHOW", "DESCRIBE", "DESC"}
WRITE_WORDS = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE)\b", re.IGNORECASE)


class ReadOnlyError(Exception):
    """The statement could change the database, and udbi only reads."""


def sqlglot_dialect(dialect: str) -> str | None:
    return SQLGLOT_DIALECTS.get(dialect)


def tokenize(text: str, dialect: str | None) -> list[Token] | None:
    """Tokens, or None when the text doesn't tokenize (e.g. an unclosed string)."""
    try:
        return sqlglot.tokenize(text, read=dialect)
    except (TokenError, ValueError):
        return None


def statement_ranges(text: str, dialect: str | None) -> list[tuple[int, int]]:
    """(start, end) of each statement, split on semicolons outside strings and comments."""
    tokens = tokenize(text, dialect)
    if tokens is None:
        semicolons = [match.start() for match in re.finditer(";", text)]
    else:
        semicolons = [token.start for token in tokens if token.token_type == TokenType.SEMICOLON]
    ranges, start = [], 0
    for position in semicolons:
        ranges.append((start, position))
        start = position + 1
    ranges.append((start, len(text)))
    return [(s, e) for s, e in ranges if text[s:e].strip()]


def statement_at(text: str, offset: int, dialect: str | None) -> str:
    """The statement under the cursor, or the one just before it."""
    ranges = statement_ranges(text, dialect)
    for start, end in ranges:
        if start <= offset <= end:
            return text[start:end].strip()
    before = [r for r in ranges if r[1] <= offset]
    chosen = before[-1] if before else (ranges[0] if ranges else None)
    return text[chosen[0] : chosen[1]].strip() if chosen else ""


def check_read_only(sql: str, dialect: str) -> None:
    """Raise ReadOnlyError unless every statement in `sql` only reads."""
    read = sqlglot_dialect(dialect)
    for start, end in statement_ranges(sql, read):
        text = sql[start:end]
        try:
            statement = sqlglot.parse_one(text, read=read)
        except (ParseError, TokenError):
            if dialect in IMPLICIT_COMMIT:
                raise ReadOnlyError(
                    "udbi couldn't check that this statement only reads, so it won't run it here."
                ) from None
            continue  # the read-only transaction and the rollback still apply
        problem = _write_problem(statement, text)
        if problem:
            raise ReadOnlyError(f"udbi is read-only: {problem}")


def _write_problem(statement: exp.Expression, text: str) -> str | None:
    if isinstance(statement, exp.Command):
        name = str(statement.this).upper()
        rest = statement.expression.name if isinstance(statement.expression, exp.Expression) else str(statement.expression or "")
        if name not in READ_COMMANDS:
            return f"{name} statements aren't allowed."
        if name == "EXPLAIN" and re.search(r"\bANALY[SZ]E\b", rest, re.IGNORECASE) and WRITE_WORDS.search(rest):
            return "EXPLAIN ANALYZE runs the statement, and this one writes."
        return None
    if isinstance(statement, exp.Pragma):
        # sqlglot rewrites table_info(x) as table_info = x, so look at what was typed.
        return "PRAGMA assignments aren't allowed." if "=" in text else None
    if isinstance(statement, (exp.Show, exp.Describe)):
        return None
    if not isinstance(statement, (exp.Query, exp.Values)):
        return f"{type(statement).__name__.upper()} statements aren't allowed."
    write = statement.find(*WRITES)
    if write is not None:
        return f"it contains a {type(write).__name__.upper()}."
    for select in statement.find_all(exp.Select):
        if select.args.get("into"):
            return "SELECT … INTO creates a table."
    return None


def syntax_problem(sql: str, dialect: str) -> str | None:
    """A short description of the first syntax error sqlglot sees, or None."""
    if not sql.strip():
        return None
    try:
        sqlglot.parse(sql, read=sqlglot_dialect(dialect))
    except ParseError as exc:
        error = exc.errors[0] if exc.errors else {}
        description = re.sub(r"<class '(?:[\w.]+\.)?(\w+)'>", r"\1", error.get("description", str(exc)))
        near = (error.get("highlight") or "").strip()
        where = f" near '{near}'" if near else ""
        return f"{description}{where} (line {error.get('line', '?')}, column {error.get('col', '?')})"
    except TokenError as exc:
        return str(exc)
    return None
