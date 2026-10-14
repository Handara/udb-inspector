"""Rich renderables shared by the CLI and the TUI."""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .database import ForeignKey, ServerInfo, TableDetails, qualified
from .detect import Target


def connection_table(target: Target, info: ServerInfo) -> Table:
    url = target.url
    rows = [
        ("Database", f"{info.product} {info.version}".strip()),
        ("Server", info.banner if info.banner != info.version else None),
        ("Driver", f"{info.dialect}+{info.driver}"),
        ("Name", url.database),
        ("Host", f"{url.host}:{url.port}" if url.host and url.port else url.host),
        ("User", url.username),
        ("Default schema", info.default_schema),
        ("Source", target.source),
        ("URL", target.display_url),
    ]
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(overflow="fold")
    for label, value in rows:
        if value:
            table.add_row(label, Text(str(value)))  # Text, so "[default]" isn't read as markup
    return table


def table_details(details: TableDetails, referenced_by: list[ForeignKey] | None) -> Group:
    """Everything about one table or view. `referenced_by` is None while it loads."""
    rel = details.relation
    parts: list[RenderableType] = [Text(rel.name, style="bold"), _summary(details, referenced_by)]
    if details.comment:
        parts.append(Text(details.comment, style="italic"))

    words = _words(rel)
    parts += _section(words["columns"].title(), _columns_table(details))

    if details.keys:
        keys = _grid("Kind", "Columns", "Name")
        for key in details.keys:
            keys.add_row(key.kind, key.definition, Text(key.name or "", style="dim"))
        parts += _section("Keys and indexes", keys)

    if details.foreign_keys:
        refs = _grid("Columns", "Points to", "Name")  # for Salesforce, Name is the relationship name
        for fk in details.foreign_keys:
            target = qualified(fk.ref_schema, fk.ref_table, rel.schema)
            refs.add_row(", ".join(fk.columns), Text(f"→ {target} ({', '.join(fk.ref_columns)})", style="cyan"), Text(fk.name or "", style="dim"))
        parts += _section("References", refs)

    if rel.kind != "view":
        if referenced_by is None:
            parts += _section("Referenced by", Text("loading…", style="dim italic"))
        elif not referenced_by:
            parts += _section("Referenced by", Text(f"No {words['foreign keys']} point at this {words['table']}.", style="dim"))
        else:
            incoming = _grid("Table", "Columns", "Name")
            for fk in referenced_by:
                source = qualified(fk.schema, fk.table, rel.schema)
                incoming.add_row(Text(f"← {source}", style="cyan"), ", ".join(fk.columns), Text(fk.name or "", style="dim"))
            parts += _section("Referenced by", incoming)

    if details.view_definition:
        sql = Syntax(details.view_definition.strip(), "sql", theme="ansi_dark", word_wrap=True, background_color="default")
        parts += _section("Definition", sql)

    return Group(*parts)


def _summary(details: TableDetails, referenced_by: list[ForeignKey] | None) -> Text:
    rel = details.relation
    words = _words(rel)
    facts = [f"{rel.kind} in {rel.schema}", _count(len(details.columns), words["column"])]
    if details.foreign_keys:
        facts.append(_count(len(details.foreign_keys), words["foreign key"]))
    if referenced_by:
        facts.append(f"referenced by {_count(len({(fk.schema, fk.table) for fk in referenced_by}), words['table'])}")
    return Text("  |  ".join(facts), style="dim")


def _words(rel) -> dict[str, str]:
    """Salesforce objects have fields and lookups; tables have columns and foreign keys."""
    if rel.kind == "object":
        return {"column": "field", "columns": "fields", "foreign key": "lookup", "foreign keys": "lookups", "table": "object"}
    return {"column": "column", "columns": "columns", "foreign key": "foreign key", "foreign keys": "foreign keys", "table": "table"}


def _count(number: int, word: str) -> str:
    return f"{number} {word}{'' if number == 1 else 's'}"


def _columns_table(details: TableDetails) -> Table:
    show_comments = any(column.comment for column in details.columns)
    table = _grid("Name", "Type", "Null", "Default", "Key", *(["Comment"] if show_comments else []))
    for column in details.columns:
        key = Text()
        if column.primary_key:
            key.append("PK", style="bold yellow")
        if column.references:
            key.append(f"{'  ' if key else ''}→ {column.references}", style="cyan")
        row = [
            Text(column.name, style="bold" if column.primary_key else ""),
            column.type,
            Text("yes", style="dim") if column.nullable else "",
            column.default or "",
            key,
        ]
        if show_comments:
            row.append(Text(column.comment or "", style="dim"))
        table.add_row(*row)
    return table


def _grid(*headers: str) -> Table:
    table = Table(box=None, show_edge=False, pad_edge=False, padding=(0, 4, 0, 0), header_style="dim")
    for index, header in enumerate(headers):
        last = index == len(headers) - 1
        # The last column doesn't wrap; capped, so long comments can't squeeze the others to nothing.
        table.add_column(header, no_wrap=last, overflow="ellipsis" if last else "fold", max_width=60 if last else None)
    return table


def _section(title: str, body: RenderableType) -> list[RenderableType]:
    heading = Text.assemble(("▍", "bold cyan"), (f" {title}", "bold"))
    return [Text(""), heading, Padding(body, (0, 0, 0, 3))]
