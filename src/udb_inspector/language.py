"""What the query editor needs from the language it edits: SQL in some dialect, or SOQL."""

from __future__ import annotations

from collections.abc import Callable

from . import hints, soql, soql_hints, sql
from .hints import Context, Suggestion, TableRef


class SqlLanguage:
    def __init__(self, dialect: str):
        self.dialect = dialect
        self.read = sql.sqlglot_dialect(dialect)
        self.title = hints.PRODUCTS.get(dialect, dialect)

    def statement_range_at(self, text: str, offset: int) -> tuple[int, int]:
        return sql.statement_range_at(text, offset, self.read)

    def statement_at(self, text: str, offset: int) -> str:
        return sql.statement_at(text, offset, self.read)

    def syntax_problem(self, statement: str) -> str | None:
        return sql.syntax_problem(statement, self.dialect)

    def grouping_problems(self, statement: str) -> list[str]:
        return hints.grouping_problems(statement, self.dialect)

    def context_at(self, text: str, offset: int) -> Context:
        return hints.context_at(text, offset, self.dialect)

    def suggest(
        self,
        context: Context,
        tables: list[tuple[str, str, str]],
        columns: Callable[[TableRef], list[tuple[str, str]] | None],
        quote: Callable[[str], str],
    ) -> list[Suggestion]:
        return hints.suggest(context, self.dialect, tables, columns, quote)

    def notes(self, statement: str) -> list[str]:
        return hints.notes(statement, self.dialect)

    def explain(self, statement: str, cursor: int) -> str | None:
        return hints.explain(statement, cursor, self.dialect)

    def tables_in(self, statement: str) -> list[TableRef]:
        return hints.tables_in(statement, self.read)

    def cheat_sheet(self) -> list[tuple[str, str]]:
        return hints.cheat_sheet(self.dialect)

    def heading(self, context: Context) -> str:
        """What the suggestions on the hint line are."""
        return {"table": "Tables", "statement": "Start with"}.get(
            context.kind, f"Columns of {context.qualifier}" if context.qualifier else "Suggestions"
        )

    no_scope = "Name a table after FROM to get its columns."

    def paging_note(self, rows: int, paged: bool | None, statement: str, reordered: bool) -> str:
        """What udbi does to a query to page it. paged is None before the first run."""
        if paged is None:
            return f"Results are read {rows:,} rows at a time: udbi adds LIMIT and OFFSET to your query."
        if not paged:
            return "This statement can't be paged, so it ran as typed."
        note = (
            f"udbi reads {rows:,} rows at a time by adding LIMIT/OFFSET to your query; your query isn't changed. "
            f"It asks for {rows + 1:,} to know whether there's a next page."
        )
        if sql.has_own_limit(statement, self.read):
            note += " Its own LIMIT still applies."
        if reordered:
            note += " Sort and search run in the database."
        return note


class SoqlLanguage:
    """SOQL, for Salesforce, with hints from the org's describes."""

    dialect = "salesforce"
    title = "SOQL"
    no_scope = "Name an object after FROM to get its fields."

    def __init__(self, db=None):
        self.db = db
        self._load: Callable[[TableRef], object] | None = None

    def _describe(self, name: str) -> dict | None:
        """A cached describe; one that isn't loaded yet is asked for, and the hints refresh when it arrives."""
        found = self.db.cached_describe(name) if self.db is not None else None
        if found is None and self._load is not None:
            self._load(TableRef(None, name, None))
        return found

    def statement_range_at(self, text: str, offset: int) -> tuple[int, int]:
        return soql.statement_range_at(text, offset)

    def statement_at(self, text: str, offset: int) -> str:
        return soql.statement_at(text, offset)

    def syntax_problem(self, statement: str) -> str | None:
        return soql_hints.syntax_problem(statement)

    def grouping_problems(self, statement: str) -> list[str]:
        return []

    def context_at(self, text: str, offset: int) -> soql_hints.SoqlContext:
        return soql_hints.context_at(text, offset)

    def suggest(self, context, tables, columns, quote) -> list[Suggestion]:
        self._load = columns
        labels = self.db.object_labels() if self.db is not None else {}
        objects = [(name, labels.get(name, name)) for _, name, _ in tables]
        return soql_hints.suggest(context, objects, self._describe)

    def notes(self, statement: str) -> list[str]:
        return soql_hints.notes(statement)

    def explain(self, statement: str, cursor: int) -> str | None:
        return soql_hints.explain(statement, cursor, self._describe)

    def heading(self, context) -> str:
        if context.kind == "column":
            owner = context.qualifier or (context.scope[0].name if context.scope else None)
            return f"Fields of {owner}" if owner else "Suggestions"
        return {"table": "Children" if getattr(context, "parent", None) else "Objects", "statement": "Start with",
                "value": f"Values of {getattr(context, 'subject', '')}", "keyword": "Next", "scope": "Scopes"}.get(context.kind, "Suggestions")

    def tables_in(self, statement: str) -> list[TableRef]:
        query = soql.parse(statement)
        return [TableRef(None, query.object, None)] if query is not None and query.object else []

    def cheat_sheet(self) -> list[tuple[str, str]]:
        return soql_hints.CHEAT_SHEET

    def paging_note(self, rows: int, paged: bool | None, statement: str, reordered: bool) -> str:
        if paged is None:
            return f"Results are read {rows:,} records at a time, through Salesforce's query cursor."
        if not paged:
            return "This query has its own LIMIT, groups or counts its records, so it ran as typed."
        note = (
            f"udbi reads {rows:,} records at a time through Salesforce's query cursor, which hands them out "
            "in batches of up to 2,000; your query isn't changed."
        )
        if reordered:
            note += " Sort and search are added to it as ORDER BY and WHERE; search looks in text fields."
        return note


def language_for(dialect: str, db=None) -> SqlLanguage | SoqlLanguage:
    return SoqlLanguage(db) if dialect == "salesforce" else SqlLanguage(dialect)
