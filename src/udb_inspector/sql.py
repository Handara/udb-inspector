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
    start, end = statement_range_at(text, offset, dialect)
    return text[start:end].strip()


def statement_range_at(text: str, offset: int, dialect: str | None) -> tuple[int, int]:
    """(start, end) of the statement under the cursor, or of the one just before it."""
    ranges = statement_ranges(text, dialect)
    for start, end in ranges:
        if start <= offset <= end:
            return start, end
    before = [r for r in ranges if r[1] <= offset]
    return before[-1] if before else (ranges[0] if ranges else (offset, offset))


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


# Paging: run one page of a query at a time instead of fetching all of it.


def single_query(sql: str, read: str | None) -> exp.Query | None:
    """The statement as a query sqlglot can rewrite, or None (several statements, SHOW, PRAGMA, …)."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=read) if s is not None]
    except (ParseError, TokenError):
        return None
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        return None
    return statements[0]


def paged_query(
    sql: str,
    read: str | None,
    *,
    columns: list[str] = (),
    sort: tuple[str, bool] | None = None,
    search: str = "",
    offset: int = 0,
    limit: int | None = None,
) -> str | None:
    """`sql` limited to one page, sorted by a result column and filtered to rows where
    any column contains `search`, written for the database. None if it can't be."""
    query = single_query(sql, read)
    if query is None:
        return None
    if search or sort or _limited(query):
        page = _wrapped(query, read, keep_order=not sort)
        if search:
            page = page.where(_contains(columns, search, read))
        if sort:
            column, descending = sort
            page = page.order_by(exp.Ordered(this=_result_column(column), desc=descending, nulls_first=False))
    else:
        # Limit the query itself, so its own ORDER BY applies as written.
        page = query.copy()
    if limit is not None:
        page = page.limit(limit)
        if offset:
            page = page.offset(offset)
            if read == "tsql" and not page.args.get("order"):
                # SQL Server's OFFSET needs an ORDER BY; this one keeps the existing order.
                page = page.order_by(exp.Ordered(this=exp.Paren(this=exp.select(exp.Null())), nulls_first=True))
    return page.sql(dialect=read)


def count_query(sql: str, read: str | None, *, columns: list[str] = (), search: str = "") -> str | None:
    """SELECT COUNT(*) of what paged_query() pages through."""
    query = single_query(sql, read)
    if query is None:
        return None
    counted = _wrapped(query, read, keep_order=False).select(exp.Count(this=exp.Star()), append=False)
    if search:
        counted = counted.where(_contains(columns, search, read))
    return counted.sql(dialect=read)


def has_own_limit(sql: str, read: str | None) -> bool:
    """Whether the query limits its rows itself (LIMIT, TOP, OFFSET, FETCH)."""
    query = single_query(sql, read)
    return query is not None and _limited(query)


def _limited(query: exp.Query) -> bool:
    return any(query.args.get(key) for key in ("limit", "offset", "fetch"))


def _wrapped(query: exp.Query, read: str | None, *, keep_order: bool) -> exp.Select:
    """SELECT * FROM (query) AS q, so a filter and an order can go around it."""
    inner = query.copy()
    with_ = inner.args.get("with_")
    if with_ is not None:  # SQL Server doesn't allow WITH inside a subquery
        inner.set("with_", None)
    limited = _limited(inner)
    # SQL Server rejects ORDER BY in a subquery unless it's limited; when we sort, the
    # inner order doesn't matter anyway.
    if not limited and (not keep_order or read == "tsql"):
        inner.set("order", None)
    outer = exp.select("*").from_(inner.subquery("q"))
    if with_ is not None:
        outer.set("with_", with_)
    return outer


def _result_column(name: str) -> exp.Column:
    """q."name": the column quoted, since a result can have any name, but q left bare
    the way _wrapped() writes it. Oracle reads a bare q as Q, which "q" isn't."""
    return exp.Column(this=exp.to_identifier(name, quoted=True), table=exp.to_identifier("q"))


def _contains(columns: list[str], search: str, read: str | None) -> exp.Expression:
    """Any column, as lowercase text, contains `search`."""
    text_type = exp.DataType.build("VARCHAR(4000)" if read == "oracle" else "TEXT")  # Oracle can't make CLOBs of numbers
    pattern = exp.Literal.string(f"%{search.lower()}%")
    return exp.or_(
        *[
            exp.Like(this=exp.Lower(this=exp.Cast(this=_result_column(name), to=text_type)), expression=pattern)
            for name in columns
        ]
    )
