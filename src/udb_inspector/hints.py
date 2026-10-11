"""Completions and syntax hints for the query editor, per database."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
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


# What each suggestion does, shown next to it in the list. A dict value varies by database.
DOCS: dict[str, str | dict[str, str]] = {
    # starting a statement
    "SELECT": "read rows: SELECT columns FROM table",
    "WITH": "name a subquery to use further down (a CTE)",
    "EXPLAIN": "show how the database will run the query",
    "VALUES": "a literal list of rows",
    "SHOW": {"postgresql": "show a setting's value", "*": "list tables, columns or settings"},
    "DESCRIBE": "list a table's columns",
    "PRAGMA": "read SQLite settings and table information",
    "SUMMARIZE": "statistics for every column of a table",
    # clauses
    "FROM": "the table or tables to read from",
    "WHERE": "keep only the rows matching a condition",
    "AND": "both conditions have to hold",
    "OR": "either condition may hold",
    "NOT": "the opposite of the condition",
    "IN": "equal to any value in a list or subquery",
    "IS NULL": "the value is missing (= NULL never matches)",
    "IS NOT NULL": "the value is present",
    "LIKE": "text pattern: % for any run of characters, _ for one",
    "BETWEEN": "within a range, both ends included",
    "EXISTS": "true when the subquery returns any row",
    "JOIN": "same as INNER JOIN",
    "LEFT JOIN": "all rows on the left, with matches (or NULLs) from the right",
    "INNER JOIN": "only rows that have a match on both sides",
    "RIGHT JOIN": "all rows on the right, with matches (or NULLs) from the left",
    "FULL JOIN": "all rows from both sides, with NULLs where there's no match",
    "CROSS JOIN": "every row paired with every row of the other table",
    "ON": "the condition that pairs rows in a join",
    "AS": "give a column or table another name",
    "DISTINCT": "drop duplicate rows",
    "GROUP BY": "one row per group; aggregate the other columns",
    "HAVING": "filter whole groups, after GROUP BY",
    "ORDER BY": "sort the result",
    "ASC": "smallest first (the default)",
    "DESC": "largest first",
    "CASE": "if/then: CASE WHEN … THEN … ELSE … END",
    "WHEN": "a condition inside CASE",
    "THEN": "the value when the WHEN condition holds",
    "ELSE": "the value when no WHEN condition holds",
    "END": "closes CASE",
    "UNION": "stack two results, dropping duplicate rows",
    "UNION ALL": "stack two results, keeping every row",
    "LIMIT": "keep only the first n rows",
    "OFFSET": {"mssql": "skip rows: ORDER BY … OFFSET n ROWS", "oracle": "skip rows: OFFSET n ROWS", "*": "skip the first n rows"},
    "ILIKE": "LIKE, ignoring upper and lower case",
    "DISTINCT ON": "the first row of each group: DISTINCT ON (column)",
    "NULLS LAST": "sort NULLs after every other value",
    "FETCH FIRST": "keep the first n rows: FETCH FIRST n ROWS ONLY",
    "REGEXP": "matches a regular expression",
    "GLOB": "Unix-style pattern; unlike LIKE it's case-sensitive",
    "QUALIFY": "filter on window function results",
    "TOP": "keep the first n rows: SELECT TOP n …",
    "ROWS": "part of OFFSET n ROWS",
    "FETCH NEXT": "after OFFSET: FETCH NEXT n ROWS ONLY",
    "ROWS ONLY": "ends FETCH FIRST / NEXT n ROWS ONLY",
    "WITH (NOLOCK)": "read without waiting for locks; may see uncommitted rows",
    "ROWNUM": "the row's number; WHERE ROWNUM <= n keeps the first n",
    # functions
    "COUNT": "number of rows; COUNT(column) skips NULLs",
    "SUM": "total of the values",
    "AVG": "average of the values",
    "MIN": "smallest value",
    "MAX": "largest value",
    "COALESCE": "the first argument that isn't NULL",
    "NULLIF": "NULL when both arguments are equal",
    "CAST": "convert to another type: CAST(x AS type)",
    "LOWER": "text in lowercase",
    "UPPER": "text in uppercase",
    "TRIM": "remove spaces at both ends",
    "ROUND": "round to n decimals",
    "ABS": "absolute value",
    "NOW": "current date and time",
    "DATE_TRUNC": "cut a date down: DATE_TRUNC('month', d)",
    "EXTRACT": "one part of a date: EXTRACT(YEAR FROM d)",
    "AGE": "the interval between two dates",
    "TO_CHAR": "format a date or number as text",
    "STRING_AGG": "join a group's values into one text",
    "ARRAY_AGG": "collect a group's values into an array",
    "LENGTH": "number of characters",
    "SUBSTRING": "part of a text: SUBSTRING(text, start, length)",
    "CONCAT": "join texts together",
    "CURDATE": "today's date",
    "DATE_FORMAT": "format a date: DATE_FORMAT(d, '%Y-%m-%d')",
    "DATEDIFF": {"mssql": "difference in a unit: DATEDIFF(day, a, b)", "*": "days between two dates: DATEDIFF(a, b)"},
    "GROUP_CONCAT": "join a group's values into one text",
    "IFNULL": "the second argument when the first is NULL",
    "DATE": "the date part, or date arithmetic: date(d, '+1 day')",
    "DATETIME": "date and time as text",
    "STRFTIME": {"duckdb": "format a date: strftime(d, '%Y-%m-%d')", "*": "format a date: strftime('%Y-%m-%d', d)"},
    "SUBSTR": "part of a text: SUBSTR(text, start, length)",
    "JSON_EXTRACT": "a value from JSON: json_extract(doc, '$.key')",
    "LIST": "collect a group's values into a list",
    "REGEXP_MATCHES": "whether text matches a regular expression",
    "GETDATE": "current date and time",
    "SYSDATETIME": "current date and time, more precise",
    "DATEADD": "add to a date: DATEADD(day, 1, d)",
    "FORMAT": "format a value: FORMAT(d, 'yyyy-MM-dd')",
    "CONVERT": "convert to another type: CONVERT(type, x)",
    "ISNULL": "the second argument when the first is NULL",
    "LEN": "number of characters",
    "SYSDATE": "current date and time",
    "SYSTIMESTAMP": "current timestamp, with time zone",
    "NVL": "the second argument when the first is NULL",
    "TO_DATE": "read text as a date: TO_DATE('2026-01-31', 'YYYY-MM-DD')",
    "TRUNC": "cut a date or number down: TRUNC(d, 'MM')",
    "LISTAGG": "join a group's values into one text",
}


def describe(word: str, dialect: str) -> str:
    doc = DOCS.get(word, "")
    return doc.get(dialect, doc.get("*", "")) if isinstance(doc, dict) else doc


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


@dataclass(frozen=True)
class _Placed:
    keyword: int  # index of the FROM / JOIN / … token that names the table
    end: int  # index of the first token after the table and its alias
    ref: TableRef


def tables_in(statement: str, read: str | None) -> list[TableRef]:
    """Tables named after FROM / JOIN / UPDATE / INTO, with their aliases."""
    return [placed.ref for placed in _placed_tables(tokenize(statement, read) or [])]


def _placed_tables(tokens: list[Token]) -> list[_Placed]:
    placed, index, in_from = [], 0, False
    names = {T.VAR, T.IDENTIFIER}
    while index < len(tokens):
        kind = tokens[index].token_type
        keyword = index
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
        placed.append(_Placed(keyword, index, TableRef(parts[-2] if len(parts) > 1 else None, parts[-1], alias)))
    return placed


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
        return _filter([Suggestion(f"{w} ", w, "keyword", describe(w, dialect)) for w in words], context.prefix)
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
    functions = [Suggestion(f"{f}(", f"{f}()", "function", describe(f, dialect)) for f in FUNCTIONS + DIALECT_FUNCTIONS.get(dialect, [])]
    keywords = [Suggestion(f"{k} ", k, "keyword", describe(k, dialect)) for k in KEYWORDS + DIALECT_KEYWORDS.get(dialect, [])]
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
    for message in _mistakes(tokens, dialect, product):
        add(message)
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


# Explanations: what the clause under the cursor will do, in the statement's own terms.

JOIN_KINDS = {T.LEFT, T.RIGHT, T.FULL, T.INNER, T.CROSS, T.NATURAL, T.OUTER}
NULLS_LAST = {"postgresql", "oracle", "duckdb"}  # where NULLs sort by default, ascending


def explain(statement: str, cursor: int, dialect: str) -> str | None:
    """What the clause the cursor is in will do; else what the last join does."""
    tokens = tokenize(statement, sqlglot_dialect(dialect))
    if not tokens:
        return None
    placed = _placed_tables(tokens)
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index]
        if token.start >= cursor:
            continue
        kind = token.token_type
        if kind == T.JOIN:
            return _explain_join(tokens, index, placed)
        if kind == T.GROUP_BY:
            return ("GROUP BY: one result row per distinct value of the grouped columns. Every other selected "
                    "column has to be inside an aggregate, such as COUNT(*), SUM(…) or MAX(…).")
        if kind == T.HAVING:
            return ("HAVING filters whole groups after GROUP BY, so it can use aggregates: HAVING COUNT(*) > 1. "
                    "To filter rows before they're grouped, use WHERE.")
        if kind == T.ORDER_BY:
            where = "last" if dialect in NULLS_LAST else "first"
            product = PRODUCTS.get(dialect, dialect)
            return f"ORDER BY sorts the result, smallest first; add DESC for largest first. {product} puts NULLs {where} when ascending."
        if kind in (T.LIMIT, T.TOP, T.FETCH):
            ordered = any(t.token_type == T.ORDER_BY for t in tokens)
            tail = "" if ordered else " Without ORDER BY, which rows those are isn't defined and can change between runs."
            return f"{token.text.upper()} keeps only the first rows of the result.{tail}"
        if kind == T.UNION:
            if index + 1 < len(tokens) and tokens[index + 1].token_type == T.ALL:
                return "UNION ALL puts the second result under the first and keeps every row, duplicates included."
            return "UNION puts the second result under the first and removes duplicate rows, which costs a sort. UNION ALL keeps them and is faster."
        if kind == T.DISTINCT:
            return "DISTINCT removes duplicate rows, comparing all the selected columns together."
        if kind == T.WHERE:
            return "WHERE keeps the rows for which the condition is true. A comparison with NULL is never true; test with IS NULL."
        if kind in (T.SELECT, T.FROM):
            break  # nothing to say about these; fall back to the joins
    joins = [index for index, token in enumerate(tokens) if token.token_type == T.JOIN]
    return _explain_join(tokens, joins[-1], placed) if joins else None


def _explain_join(tokens: list[Token], index: int, placed: list[_Placed]) -> str:
    kinds = set()
    before = index - 1
    while before >= 0 and tokens[before].token_type in JOIN_KINDS:
        kinds.add(tokens[before].token_type)
        before -= 1
    right = next((p.ref for p in placed if p.keyword == index), None)
    left = [p.ref for p in placed if p.keyword < index]
    names = [ref.name.lower() for ref in left + ([right] if right else [])]
    twice = {name for name in names if names.count(name) > 1}  # a self-join: aliases tell the sides apart
    r = _label(right, twice) if right else "the joined table"
    l = " + ".join(_label(ref, twice) for ref in left) if left else "the tables before it"
    if T.CROSS in kinds:
        return f"CROSS JOIN: every {l} row paired with every {r} row, so rows of {l} × rows of {r}. There's no ON condition."
    if T.NATURAL in kinds:
        return (f"NATURAL JOIN: matches {l} and {r} on every column name they share. Adding a column with a shared "
                "name later silently changes the result; ON is safer.")
    if T.LEFT in kinds:
        return f"LEFT JOIN: every {l} row, next to its matching {r} rows. Where {r} has no match, its columns come back NULL."
    if T.RIGHT in kinds:
        return f"RIGHT JOIN: every {r} row, next to its matching {l} rows. Where {l} has no match, its columns come back NULL."
    if T.FULL in kinds:
        return (f"FULL JOIN: the matching {l}/{r} pairs, plus {l} rows without a match ({r} columns NULL) "
                f"and {r} rows without a match ({l} columns NULL).")
    return (f"INNER JOIN: one row for each {l} row and {r} row that match the ON condition. "
            f"Rows without a match on the other side are left out.")


def _label(ref: TableRef, twice: set[str]) -> str:
    return f"{ref.name} {ref.alias}" if ref.alias and ref.name.lower() in twice else ref.name


def _mistakes(tokens: list[Token], dialect: str, product: str) -> list[str]:
    """Common mistakes that a parser may accept but rarely mean what was intended."""
    found = []
    kinds = [token.token_type for token in tokens]
    for index, kind in enumerate(kinds):
        following = kinds[index + 1 : index + 4]
        if kind == T.COMMA and following[:1] == [T.FROM]:
            found.append("There's a comma right before FROM. Remove it, or add the column you meant.")
        elif kind in (T.EQ, T.NEQ) and following[:1] == [T.NULL]:
            found.append(f"{tokens[index].text} NULL is never true, not even for NULLs. Use IS NULL or IS NOT NULL.")
        elif kind == T.NOT and following == [T.IN, T.L_PAREN, T.SELECT]:
            found.append("NOT IN (SELECT …) returns no rows at all if the subquery returns a NULL. NOT EXISTS doesn't have that trap.")
        elif kind == T.JOIN and index and kinds[index - 1] in (T.FULL, T.OUTER) and T.FULL in kinds[max(0, index - 2) : index] and dialect in ("mysql", "mariadb"):
            found.append(f"{product} has no FULL JOIN. Combine a LEFT JOIN and a RIGHT JOIN with UNION.")

    placed = _placed_tables(tokens)
    names: dict[str, list[TableRef]] = {}
    for p in placed:
        names.setdefault(p.ref.name.lower(), []).append(p.ref)
    for refs in names.values():
        aliases = [ref.alias for ref in refs]
        if len(refs) > 1 and (None in aliases or len(set(aliases)) < len(aliases)):
            name = refs[0].name
            found.append(f"{name} is used twice. Give each side its own alias: {name} a JOIN {name} b ON a.… = b.….")
    for p in placed:
        if kinds[p.keyword] != T.JOIN or {T.CROSS, T.NATURAL} & set(kinds[max(0, p.keyword - 2) : p.keyword]):
            continue
        if p.end < len(tokens) and kinds[p.end] not in (T.ON, T.USING, T.SEMICOLON):
            found.append(f"{p.ref.name} is joined without ON, so nothing says which rows match. Add ON {p.ref.alias or p.ref.name}.… = ….")
    return found


def grouping_problems(statement: str, dialect: str) -> list[str]:
    """Selected columns that are neither grouped nor aggregated."""
    try:
        tree = sqlglot.parse_one(statement, read=sqlglot_dialect(dialect))
    except (ParseError, TokenError, ValueError):
        return []
    if tree is None:
        return []
    consequence = {
        "sqlite": "SQLite takes it from an arbitrary row",
        "mysql": "MySQL rejects it unless ONLY_FULL_GROUP_BY is turned off",
        "mariadb": "MariaDB takes it from an arbitrary row unless ONLY_FULL_GROUP_BY is on",
        "postgresql": "PostgreSQL rejects it unless the GROUP BY includes the table's primary key",
    }.get(dialect, f"{PRODUCTS.get(dialect, dialect)} rejects it")
    problems = []
    for select in tree.find_all(exp.Select):
        group = select.args.get("group")
        grouped = list(group.expressions) if group else []
        aggregated = any(e.find(exp.AggFunc) for e in select.expressions)
        if not grouped and not aggregated:
            continue
        names = {g.name.lower() for g in grouped if isinstance(g, exp.Column)}
        positions = {int(g.this) for g in grouped if isinstance(g, exp.Literal) and g.is_int}
        for position, expression in enumerate(select.expressions, 1):
            inner = expression.this if isinstance(expression, exp.Alias) else expression
            if not isinstance(inner, exp.Column) or position in positions:
                continue
            alias = expression.alias.lower() if isinstance(expression, exp.Alias) else ""
            if inner.name.lower() in names or (alias and alias in names):
                continue
            how = "isn't in GROUP BY or an aggregate" if grouped else "is mixed with an aggregate but there's no GROUP BY"
            problems.append(f"{inner.sql()} {how}: {consequence}.")
    return problems
