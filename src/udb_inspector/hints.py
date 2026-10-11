"""Completions and syntax hints for the query editor, per database."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlglot.tokens import Token, TokenType

from .sql import sqlglot_dialect, statement_ranges, tokenize

T = TokenType
TABLE_KEYWORDS = {T.FROM, T.JOIN, T.INTO, T.UPDATE, T.TABLE, T.DESCRIBE}
ENDS_EXPRESSION = {T.VAR, T.IDENTIFIER, T.NUMBER, T.STRING, T.R_PAREN, T.STAR, T.NULL, T.TRUE, T.FALSE}
COMPARISONS = {T.EQ, T.NEQ, T.LT, T.GT, T.LTE, T.GTE, T.LIKE, T.ILIKE}
NAME = re.compile(r"[A-Za-z_][\w$]*$")
QUALIFIER = re.compile(r'(?:([A-Za-z_][\w$]*)|"([^"]+)"|`([^`]+)`|\[([^\]]+)\])\.$')

PRODUCTS = {"postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB", "sqlite": "SQLite",
            "mssql": "SQL Server", "oracle": "Oracle", "duckdb": "DuckDB"}


@dataclass(frozen=True)
class TableRef:
    schema: str | None
    name: str
    alias: str | None


@dataclass(frozen=True)
class Context:
    kind: str  # "table", "column", "statement" or "none"
    prefix: str = ""
    qualifier: str | None = None
    scope: list[TableRef] = field(default_factory=list)
    after_expression: bool = False  # e.g. "SELECT id |": FROM is likelier than another column


@dataclass(frozen=True)
class Suggestion:
    text: str  # inserted
    label: str  # shown
    kind: str  # "table", "view", "column", "keyword" or "function"
    detail: str = ""


# Clause keywords valid everywhere, plus each database's row-limiting syntax.
KEYWORDS = [
    "FROM", "WHERE", "AND", "OR", "NOT", "IN", "IS NULL", "IS NOT NULL", "LIKE", "BETWEEN", "EXISTS",
    "JOIN", "LEFT JOIN", "INNER JOIN", "RIGHT JOIN", "FULL JOIN", "CROSS JOIN", "ON", "AS", "DISTINCT",
    "GROUP BY", "HAVING", "ORDER BY", "ASC", "DESC", "CASE", "WHEN", "THEN", "ELSE", "END", "UNION", "UNION ALL",
]
DIALECT_KEYWORDS = {
    "postgresql": ["LIMIT", "OFFSET", "ILIKE", "DISTINCT ON", "NULLS LAST", "FETCH FIRST"],
    "mysql": ["LIMIT", "OFFSET", "REGEXP"],
    "mariadb": ["LIMIT", "OFFSET", "REGEXP"],
    "sqlite": ["LIMIT", "OFFSET", "GLOB"],
    "duckdb": ["LIMIT", "OFFSET", "ILIKE", "QUALIFY"],
    "mssql": ["TOP", "OFFSET", "ROWS", "FETCH NEXT", "ROWS ONLY", "WITH (NOLOCK)"],
    "oracle": ["FETCH FIRST", "ROWS ONLY", "OFFSET", "ROWNUM"],
}
STATEMENT_KEYWORDS = ["SELECT", "WITH", "EXPLAIN", "VALUES"]
DIALECT_STATEMENTS = {"mysql": ["SHOW", "DESCRIBE"], "mariadb": ["SHOW", "DESCRIBE"], "sqlite": ["PRAGMA"],
                      "postgresql": ["SHOW"], "duckdb": ["DESCRIBE", "SUMMARIZE"]}

FUNCTIONS = ["COUNT", "SUM", "AVG", "MIN", "MAX", "COALESCE", "NULLIF", "CAST", "LOWER", "UPPER", "TRIM", "ROUND", "ABS"]
DIALECT_FUNCTIONS = {
    "postgresql": ["NOW", "DATE_TRUNC", "EXTRACT", "AGE", "TO_CHAR", "STRING_AGG", "ARRAY_AGG", "LENGTH", "SUBSTRING", "CONCAT"],
    "mysql": ["NOW", "CURDATE", "DATE_FORMAT", "DATEDIFF", "GROUP_CONCAT", "IFNULL", "CONCAT", "LENGTH", "SUBSTRING"],
    "mariadb": ["NOW", "CURDATE", "DATE_FORMAT", "DATEDIFF", "GROUP_CONCAT", "IFNULL", "CONCAT", "LENGTH", "SUBSTRING"],
    "sqlite": ["DATE", "DATETIME", "STRFTIME", "IFNULL", "GROUP_CONCAT", "LENGTH", "SUBSTR", "JSON_EXTRACT"],
    "duckdb": ["NOW", "DATE_TRUNC", "STRFTIME", "STRING_AGG", "LIST", "LENGTH", "SUBSTRING", "REGEXP_MATCHES"],
    "mssql": ["GETDATE", "SYSDATETIME", "DATEADD", "DATEDIFF", "FORMAT", "CONVERT", "ISNULL", "LEN", "SUBSTRING", "STRING_AGG", "CONCAT"],
    "oracle": ["SYSDATE", "SYSTIMESTAMP", "NVL", "TO_CHAR", "TO_DATE", "TRUNC", "LISTAGG", "LENGTH", "SUBSTR"],
}


def context_at(text: str, offset: int, dialect: str) -> Context:
    """What could be typed at `offset`."""
    read = sqlglot_dialect(dialect)
    start, end = next(((s, e) for s, e in statement_ranges(text, read) if s <= offset <= e), (offset, offset))
    statement = text[start:end] if start < offset else ""
    before = text[start:offset] if start < offset else ""
    # A statement starts after the previous semicolon, even if it's still empty.
    before = before[before.rfind(";") + 1 :] if ";" in before else before

    prefix_match = NAME.search(before)
    prefix = prefix_match.group(0) if prefix_match else ""
    head = before[: len(before) - len(prefix)]
    qualifier_match = QUALIFIER.search(head)
    qualifier = next((g for g in qualifier_match.groups() if g), None) if qualifier_match else None
    if qualifier_match:
        head = head[: qualifier_match.start()]

    tokens = tokenize(head, read)
    if tokens is None or _in_comment(head, tokens):
        return Context("none")
    scope = tables_in(statement or before, read)
    if qualifier:
        return Context("column", prefix, qualifier, scope)
    if not tokens:
        return Context("statement", prefix)
    previous = tokens[-1].token_type
    clause = next((t.token_type for t in reversed(tokens) if t.token_type in TABLE_KEYWORDS | {T.SELECT, T.WHERE, T.ON, T.ORDER_BY, T.GROUP_BY, T.HAVING, T.SET}), None)
    if previous in TABLE_KEYWORDS or (previous == T.COMMA and clause == T.FROM):
        return Context("table", prefix, None, scope)
    return Context("column", prefix, None, scope, after_expression=previous in ENDS_EXPRESSION)


def _in_comment(head: str, tokens: list[Token]) -> bool:
    last_end = tokens[-1].end + 1 if tokens else 0
    tail = head[last_end:]
    return "--" in tail.split("\n")[-1] or ("/*" in tail and "*/" not in tail)


def tables_in(statement: str, read: str | None) -> list[TableRef]:
    """Tables named after FROM / JOIN / UPDATE / INTO, with their aliases."""
    tokens = tokenize(statement, read) or []
    refs, index, in_from = [], 0, False
    names = {T.VAR, T.IDENTIFIER}
    while index < len(tokens):
        kind = tokens[index].token_type
        starts = kind in {T.FROM, T.JOIN, T.UPDATE, T.INTO} or (kind == T.COMMA and in_from)
        if kind in {T.FROM, T.JOIN}:
            in_from = True
        elif kind in {T.WHERE, T.GROUP_BY, T.ORDER_BY, T.HAVING, T.ON, T.LIMIT, T.R_PAREN}:
            in_from = False
        index += 1
        if not starts or index >= len(tokens) or tokens[index].token_type not in names:
            continue
        parts = [tokens[index].text]
        index += 1
        while index + 1 < len(tokens) and tokens[index].token_type == T.DOT and tokens[index + 1].token_type in names:
            parts.append(tokens[index + 1].text)
            index += 2
        alias = None
        if index < len(tokens) and tokens[index].token_type == T.ALIAS:
            index += 1
        if index < len(tokens) and tokens[index].token_type in names:
            alias = tokens[index].text
            index += 1
        refs.append(TableRef(parts[-2] if len(parts) > 1 else None, parts[-1], alias))
    return refs


def suggest(
    context: Context,
    dialect: str,
    tables: list[tuple[str, str, str]],
    columns: Callable[[TableRef], list[tuple[str, str]] | None],
    quote: Callable[[str], str] = lambda name: name,
) -> list[Suggestion]:
    """Ranked suggestions. `tables` is (schema label or "", name, kind); `columns` returns
    (name, type) pairs, or None while they are still loading."""
    if context.kind == "none":
        return []
    if context.kind == "statement":
        words = STATEMENT_KEYWORDS + DIALECT_STATEMENTS.get(dialect, [])
        return _filter([Suggestion(f"{w} ", w, "keyword") for w in words], context.prefix)
    if context.kind == "table":
        found = [
            Suggestion(f"{quote(schema)}.{quote(name)}" if schema else quote(name), f"{schema}.{name}" if schema else name, kind)
            for schema, name, kind in tables
        ]
        return _filter(found, context.prefix)

    refs = context.scope
    if context.qualifier:
        wanted = context.qualifier.lower()
        refs = [r for r in refs if (r.alias or "").lower() == wanted or r.name.lower() == wanted]
    column_hits = []
    for ref in refs:
        for name, type_ in columns(ref) or []:
            detail = type_ if len(refs) == 1 or context.qualifier else f"{type_}, {ref.alias or ref.name}"
            column_hits.append(Suggestion(quote(name), name, "column", detail))
    if context.qualifier:
        return _filter(column_hits, context.prefix)
    functions = [Suggestion(f"{f}(", f"{f}()", "function") for f in FUNCTIONS + DIALECT_FUNCTIONS.get(dialect, [])]
    keywords = [Suggestion(f"{k} ", k, "keyword") for k in KEYWORDS + DIALECT_KEYWORDS.get(dialect, [])]
    ordered = keywords + column_hits + functions if context.after_expression else column_hits + functions + keywords
    return _filter(ordered, context.prefix)


def _filter(items: list[Suggestion], prefix: str) -> list[Suggestion]:
    """Prefix matches first, then other substring matches; no duplicates."""
    needle = prefix.lower()
    seen, starts, contains = set(), [], []
    for item in items:
        key = (item.label.lower(), item.kind)
        if key in seen:
            continue
        seen.add(key)
        label = item.label.lower()
        if label.startswith(needle):
            starts.append(item)
        elif needle and needle in label:
            contains.append(item)
    exact = [s for s in starts if s.label.lower() == needle]
    return ([] if exact and len(starts) == 1 else starts) + contains


def notes(statement: str, dialect: str) -> list[str]:
    """Syntax from another database that won't work (or works differently) here."""
    tokens = tokenize(statement, sqlglot_dialect(dialect)) or []
    product = PRODUCTS.get(dialect, dialect)
    found: list[str] = []

    def add(message: str) -> None:
        if message not in found:
            found.append(message)

    for index, token in enumerate(tokens):
        kind, word = token.token_type, token.text.upper()
        following = tokens[index + 1].token_type if index + 1 < len(tokens) else None
        previous = tokens[index - 1].token_type if index else None
        if kind == T.LIMIT and dialect == "mssql":
            add("SQL Server has no LIMIT. Use SELECT TOP 10 … or ORDER BY … OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY.")
        elif kind == T.LIMIT and dialect == "oracle":
            add("Oracle has no LIMIT. Use FETCH FIRST 10 ROWS ONLY (12c and later) or WHERE ROWNUM <= 10.")
        elif (kind == T.TOP or (word == "TOP" and previous == T.SELECT)) and dialect != "mssql":
            add("TOP is SQL Server syntax. " + ("Use FETCH FIRST 10 ROWS ONLY." if dialect == "oracle" else "Use LIMIT 10 at the end."))
        elif kind == T.ILIKE and dialect not in ("postgresql", "duckdb"):
            if dialect in ("mysql", "mariadb"):
                add(f"{product} has no ILIKE; LIKE already ignores case with the default collation.")
            elif dialect == "sqlite":
                add("SQLite has no ILIKE; LIKE already ignores case for ASCII letters.")
            else:
                add(f"{product} has no ILIKE. Use LOWER(column) LIKE LOWER('…').")
        elif kind == T.DPIPE and dialect in ("mysql", "mariadb"):
            add(f"In {product}, || means OR unless PIPES_AS_CONCAT is set. Use CONCAT(a, b) to join text.")
        elif kind == T.DPIPE and dialect == "mssql":
            add("SQL Server joins text with + or CONCAT(a, b), not ||.")
        elif kind == T.IDENTIFIER and previous in COMPARISONS and dialect not in ("mysql", "mariadb"):
            add(f'In {product}, "{token.text}" names a column. Put text values in single quotes: \'{token.text}\'.')
        elif following == T.L_PAREN:
            hint = _function_hint(word, dialect, product)
            if hint:
                add(hint)
    if "`" in statement and dialect not in ("mysql", "mariadb", "sqlite"):
        add(f"{product} doesn't quote names with backticks. Use " + ('[brackets] or "double quotes".' if dialect == "mssql" else '"double quotes".'))
    return found


def _function_hint(name: str, dialect: str, product: str) -> str | None:
    if name == "NOW" and dialect == "mssql":
        return "SQL Server has no NOW(). Use GETDATE() or SYSDATETIME()."
    if name == "NOW" and dialect == "oracle":
        return "Oracle has no NOW(). Use SYSDATE or SYSTIMESTAMP."
    if name == "GETDATE" and dialect != "mssql":
        return f"GETDATE() is SQL Server's. In {product} use CURRENT_TIMESTAMP."
    if name in ("ISNULL", "IFNULL", "NVL") and not _native_null_function(name, dialect):
        return f"{name}() isn't {product}'s. COALESCE(a, b) works everywhere."
    if name == "LEN" and dialect not in ("mssql", "duckdb"):
        return f"LEN() is SQL Server's. In {product} use LENGTH()."
    if name == "LENGTH" and dialect == "mssql":
        return "SQL Server uses LEN() (or DATALENGTH() for bytes)."
    if name == "SUBSTR" and dialect == "mssql":
        return "SQL Server uses SUBSTRING(text, start, length)."
    return None


def _native_null_function(name: str, dialect: str) -> bool:
    return {"ISNULL": {"mssql"}, "IFNULL": {"mysql", "mariadb", "sqlite", "duckdb"}, "NVL": {"oracle"}}[name].__contains__(dialect)


# Quick reference shown with F1.
CHEAT_SHEET_TOPICS = [
    "First 10 rows", "Rows 21–30", "Quote a name", "Join text", "Now", "Fallback for NULL",
    "Case-insensitive match", "Start of the month", "Format a date", "Join values of a group", "List tables",
]
CHEAT_SHEETS = {
    "postgresql": [
        "SELECT … LIMIT 10", "SELECT … LIMIT 10 OFFSET 20", '"Order Lines"', "a || b   or   CONCAT(a, b)", "NOW()",
        "COALESCE(a, b)", "name ILIKE 'bob%'", "DATE_TRUNC('month', created_at)", "TO_CHAR(created_at, 'YYYY-MM-DD')",
        "STRING_AGG(name, ', ')", "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
    ],
    "mysql": [
        "SELECT … LIMIT 10", "SELECT … LIMIT 20, 10", "`Order Lines`", "CONCAT(a, b)", "NOW()",
        "COALESCE(a, b)   or   IFNULL(a, b)", "name LIKE 'bob%'  (ignores case by default)", "DATE_FORMAT(created_at, '%Y-%m-01')",
        "DATE_FORMAT(created_at, '%Y-%m-%d')", "GROUP_CONCAT(name SEPARATOR ', ')", "SHOW TABLES",
    ],
    "sqlite": [
        "SELECT … LIMIT 10", "SELECT … LIMIT 10 OFFSET 20", '"Order Lines"', "a || b", "datetime('now')",
        "COALESCE(a, b)   or   IFNULL(a, b)", "name LIKE 'bob%'  (ignores case for ASCII)", "date(created_at, 'start of month')",
        "strftime('%Y-%m-%d', created_at)", "group_concat(name, ', ')", "SELECT name FROM sqlite_master WHERE type = 'table'",
    ],
    "duckdb": [
        "SELECT … LIMIT 10", "SELECT … LIMIT 10 OFFSET 20", '"Order Lines"', "a || b   or   CONCAT(a, b)", "now()",
        "COALESCE(a, b)", "name ILIKE 'bob%'", "date_trunc('month', created_at)", "strftime(created_at, '%Y-%m-%d')",
        "string_agg(name, ', ')", "SHOW TABLES",
    ],
    "mssql": [
        "SELECT TOP 10 …", "ORDER BY … OFFSET 20 ROWS FETCH NEXT 10 ROWS ONLY", "[Order Lines]", "a + b   or   CONCAT(a, b)",
        "GETDATE()   or   SYSDATETIME()", "COALESCE(a, b)   or   ISNULL(a, b)", "LOWER(name) LIKE 'bob%'  (or a CI collation)",
        "DATEFROMPARTS(YEAR(d), MONTH(d), 1)", "FORMAT(created_at, 'yyyy-MM-dd')", "STRING_AGG(name, ', ')",
        "SELECT name FROM sys.tables",
    ],
    "oracle": [
        "SELECT … FETCH FIRST 10 ROWS ONLY", "… OFFSET 20 ROWS FETCH NEXT 10 ROWS ONLY", '"Order Lines"', "a || b",
        "SYSDATE   or   SYSTIMESTAMP", "COALESCE(a, b)   or   NVL(a, b)", "LOWER(name) LIKE 'bob%'", "TRUNC(created_at, 'MM')",
        "TO_CHAR(created_at, 'YYYY-MM-DD')", "LISTAGG(name, ', ') WITHIN GROUP (ORDER BY name)", "SELECT table_name FROM user_tables",
    ],
}
CHEAT_SHEETS["mariadb"] = CHEAT_SHEETS["mysql"]


def cheat_sheet(dialect: str) -> list[tuple[str, str]]:
    return list(zip(CHEAT_SHEET_TOPICS, CHEAT_SHEETS.get(dialect, CHEAT_SHEETS["postgresql"])))
