"""Just enough SOQL: statements, clauses, the select list, and the rewrites paging needs.

SOQL is close to SQL but not close enough for sqlglot: date literals like
LAST_N_DAYS:30, backslash escapes in strings, relationship paths (Account.Name),
child subqueries in the select list, TYPEOF, FIELDS(ALL).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .sql import ReadOnlyError

TOKEN = re.compile(
    r"""
    (?P<string>'(?:\\.|[^'\\])*'?)
  | (?P<datetime>\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))?)
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?::\d+)?)
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<op><=|>=|!=|<>|[=<>(),;:.*+-])
  | (?P<space>\s+)
  | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)

# Clause keywords at the top level, and the word that must follow the ones that need one.
CLAUSES = {"SELECT": None, "FROM": None, "WHERE": None, "WITH": None, "HAVING": None, "LIMIT": None, "OFFSET": None,
           "GROUP": "BY", "ORDER": "BY", "USING": "SCOPE", "FOR": ("VIEW", "REFERENCE", "UPDATE"),
           "UPDATE": ("TRACKING", "VIEWSTAT"), "ALL": "ROWS"}
AGGREGATES = {
    "AVG", "COUNT", "COUNT_DISTINCT", "MIN", "MAX", "SUM", "GROUPING",
    "CALENDAR_MONTH", "CALENDAR_QUARTER", "CALENDAR_YEAR", "DAY_IN_MONTH", "DAY_IN_WEEK", "DAY_IN_YEAR", "DAY_ONLY",
    "FISCAL_MONTH", "FISCAL_QUARTER", "FISCAL_YEAR", "HOUR_IN_DAY", "WEEK_IN_MONTH", "WEEK_IN_YEAR",
}
FIELD_FUNCTIONS = {"TOLABEL", "FORMAT", "CONVERTCURRENCY", "CONVERTTIMEZONE"}  # a field's value, still named after it


@dataclass(frozen=True)
class Token:
    kind: str  # string, datetime, word, number, op, other
    text: str
    start: int
    end: int
    depth: int  # parentheses around it

    @property
    def upper(self) -> str:
        return self.text.upper()


@dataclass(frozen=True)
class Item:
    """One entry of the select list, and how its value comes back."""

    kind: str  # "field", "subquery", "aggregate", "count", or "fields"/"typeof" (columns found in the records)
    text: str
    column: str  # the result column
    path: tuple[str, ...] = ()  # where the value sits in a record: ("Account", "Name")


@dataclass(frozen=True)
class Query:
    text: str
    tokens: list[Token]
    clauses: dict[str, tuple[int, int]]  # clause -> character span, keyword included
    items: list[Item]
    object: str | None

    def has(self, clause: str) -> bool:
        return clause in self.clauses

    @property
    def counts_only(self) -> bool:
        return any(item.kind == "count" for item in self.items)

    @property
    def discovers_columns(self) -> bool:
        return any(item.kind in ("fields", "typeof") for item in self.items)

    @property
    def pageable(self) -> bool:
        """Whether udbi can add ORDER BY and WHERE and page through it with the query cursor."""
        if self.counts_only or any(item.kind == "aggregate" for item in self.items):
            return False
        return not any(self.has(clause) for clause in ("LIMIT", "OFFSET", "GROUP"))

    def fields(self) -> list[Item]:
        return [item for item in self.items if item.kind == "field"]


def tokens(text: str) -> list[Token]:
    found, depth = [], 0
    for match in TOKEN.finditer(text):
        kind = match.lastgroup
        if kind == "space":
            continue
        value = match.group()
        if value == ")":
            depth = max(0, depth - 1)
        found.append(Token(kind, value, match.start(), match.end(), depth))
        if value == "(":
            depth += 1
    return found


def statement_ranges(text: str) -> list[tuple[int, int]]:
    """Statements separated by semicolons outside strings."""
    ranges, start = [], 0
    for token in tokens(text):
        if token.text == ";":
            ranges.append((start, token.start))
            start = token.end
    ranges.append((start, len(text)))
    return [(begin, end) for begin, end in ranges if text[begin:end].strip()] or [(0, len(text))]


def statement_range_at(text: str, offset: int) -> tuple[int, int]:
    ranges = statement_ranges(text)
    for begin, end in ranges:
        if offset <= end:
            return begin, end
    return ranges[-1]


def statement_at(text: str, offset: int) -> str:
    begin, end = statement_range_at(text, offset)
    return text[begin:end].strip()


def parse(text: str) -> Query | None:
    """A SELECT, or None for anything else."""
    text = text.strip().rstrip(";").strip()
    toks = tokens(text)
    if not toks or toks[0].upper != "SELECT":
        return None
    clauses = _clauses(text, toks)
    if "FROM" not in clauses:
        return None
    from_tokens = [token for token in toks if clauses["FROM"][0] <= token.start < clauses["FROM"][1]]
    obj = from_tokens[1].text if len(from_tokens) > 1 and from_tokens[1].kind == "word" else None
    select = clauses["SELECT"]
    return Query(text, toks, clauses, _items(text, [t for t in toks if select[0] <= t.start < select[1]][1:]), obj)


def _clauses(text: str, toks: list[Token]) -> dict[str, tuple[int, int]]:
    starts: list[tuple[str, int]] = []
    for index, token in enumerate(toks):
        if token.depth or token.kind != "word" or token.upper not in CLAUSES:
            continue
        follower = CLAUSES[token.upper]
        if follower is not None:
            following = toks[index + 1].upper if index + 1 < len(toks) else ""
            if following not in ((follower,) if isinstance(follower, str) else follower):
                continue
        if token.upper == "FROM" and any(name == "FROM" for name, _ in starts):
            continue  # FROM only names the object once
        starts.append((token.upper, token.start))
    spans = {}
    for index, (name, start) in enumerate(starts):
        end = starts[index + 1][1] if index + 1 < len(starts) else len(text)
        spans.setdefault(name, (start, end))
    return spans


def _items(text: str, toks: list[Token]) -> list[Item]:
    groups: list[list[Token]] = [[]]
    base = toks[0].depth if toks else 0
    for token in toks:
        if token.text == "," and token.depth == base:
            groups.append([])
        else:
            groups[-1].append(token)
    items, unnamed = [], 0
    for group in groups:
        if not group:
            continue
        item_text = text[group[0].start : group[-1].end]
        first = group[0].upper
        if group[0].text == "(":
            inner = [token for token in group if token.depth == group[0].depth + 1]
            after_from = next((inner[i + 1].text for i, t in enumerate(inner[:-1]) if t.upper == "FROM"), item_text)
            items.append(Item("subquery", item_text, after_from, (after_from,)))
        elif first == "FIELDS":
            items.append(Item("fields", item_text, item_text))
        elif first == "TYPEOF":
            items.append(Item("typeof", item_text, group[1].text if len(group) > 1 else item_text))
        elif len(group) > 1 and group[1].text == "(":
            alias = group[-1].text if group[-1].kind == "word" and group[-2].text == ")" else None
            if first == "COUNT" and len(group) in (3, 4) and group[2].text == ")":
                items.append(Item("count", item_text, alias or "count"))
            elif first in AGGREGATES:
                column = alias or f"expr{unnamed}"
                unnamed += alias is None
                items.append(Item("aggregate", item_text, column, (column,)))
            else:  # toLabel(Status), FORMAT(Amount): the field's value under its own name
                inner = group[2].text if len(group) > 2 and group[2].kind == "word" else item_text
                items.append(Item("field", item_text, alias or inner, tuple((alias or inner).split("."))))
        else:
            path = group[0].text
            items.append(Item("field", item_text, path, tuple(path.split("."))))
    return items


def check_read_only(query: Query) -> None:
    """SOQL can't change data, but these clauses update view statistics or lock records."""
    if query.has("FOR"):
        raise ReadOnlyError("FOR VIEW, FOR REFERENCE and FOR UPDATE update records or lock them; udbi only reads.")
    if query.has("UPDATE"):
        raise ReadOnlyError("UPDATE TRACKING and UPDATE VIEWSTAT record statistics; udbi only reads.")


def all_rows(query: Query) -> bool:
    """ALL ROWS: deleted and archived records too (the queryAll resource)."""
    return query.has("ALL")


def without(query: Query, *names: str) -> str:
    """The query with these clauses taken out."""
    spans = sorted(query.clauses[name] for name in names if name in query.clauses)
    text, removed = query.text, 0
    for start, end in spans:
        text = text[: start - removed] + text[end - removed :]
        removed += end - start
    return " ".join(text.split()) if spans else text


def with_order(query: Query, path: str, descending: bool) -> str:
    unordered = parse(without(query, "ORDER"))
    clause = f"ORDER BY {path} {'DESC' if descending else 'ASC'} NULLS LAST"
    return _insert_before(unordered, ("LIMIT", "OFFSET", "FOR", "UPDATE", "ALL"), clause)


def with_condition(query: Query, condition: str) -> str:
    """WHERE (its own conditions) AND (condition)."""
    if query.has("WHERE"):
        start, end = query.clauses["WHERE"]
        own = query.text[start + len("WHERE") : end].strip()
        return f"{query.text[:start]}WHERE ({own}) AND ({condition}) {query.text[end:]}".strip()
    return _insert_before(query, ("WITH", "GROUP", "ORDER", "LIMIT", "OFFSET", "FOR", "UPDATE", "ALL"), f"WHERE {condition}")


def _insert_before(query: Query, clauses: tuple[str, ...], addition: str) -> str:
    starts = [query.clauses[name][0] for name in clauses if name in query.clauses]
    at = min(starts) if starts else len(query.text)
    return f"{query.text[:at].rstrip()} {addition} {query.text[at:]}".strip()


def literal(value: str) -> str:
    """A SOQL string literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def like_pattern(search: str) -> str:
    """'%search%' with SOQL's LIKE wildcards in the search taken literally."""
    escaped = search.replace("\\", "\\\\").replace("'", "\\'").replace("%", "\\%").replace("_", "\\_")
    return f"'%{escaped}%'"
