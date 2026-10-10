"""Rich renderables shared by the CLI and the TUI."""

from __future__ import annotations

from rich import box
from rich.console import Group, RenderableType
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
        ("Detected from", target.source),
        ("URL", target.display_url),
    ]
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(overflow="fold")
    for label, value in rows:
        if value:
            table.add_row(label, str(value))
    return table


def table_details(details: TableDetails, referenced_by: list[ForeignKey] | None) -> Group:
    """Everything about one table or view. `referenced_by` is None while it loads."""
    rel = details.relation
    parts: list[RenderableType] = [Text.assemble((rel.name, "bold"), (f"  {rel.kind} in {rel.schema}", "dim"))]
    if details.comment:
        parts.append(Text(details.comment, style="italic"))

    parts += _section(f"Columns ({len(details.columns)})", _columns_table(details))

    if details.keys:
        keys = _grid("Kind", "Name", "Definition")
        for key in details.keys:
            keys.add_row(key.kind, key.name or "", key.definition)
        parts += _section("Keys and indexes", keys)

    if details.foreign_keys:
        refs = _grid("Columns", "References", "Name")
        for fk in details.foreign_keys:
            target = qualified(fk.ref_schema, fk.ref_table, rel.schema)
            refs.add_row(", ".join(fk.columns), f"{target} ({', '.join(fk.ref_columns)})", fk.name or "")
        parts += _section("References", refs)

    if rel.kind == "table":
        if referenced_by is None:
            parts += _section("Referenced by", Text("loading…", style="dim italic"))
        elif not referenced_by:
            parts += _section("Referenced by", Text("no foreign keys point at this table", style="dim"))
        else:
            incoming = _grid("Table", "Columns", "Name")
            for fk in referenced_by:
                incoming.add_row(qualified(fk.schema, fk.table, rel.schema), ", ".join(fk.columns), fk.name or "")
            parts += _section("Referenced by", incoming)

    if details.view_definition:
        sql = Syntax(details.view_definition.strip(), "sql", theme="ansi_dark", word_wrap=True, background_color="default")
        parts += _section("Definition", sql)

    return Group(*parts)


def _columns_table(details: TableDetails) -> Table:
    show_comments = any(column.comment for column in details.columns)
    table = _grid("Name", "Type", "Null", "Default", "Key", *(["Comment"] if show_comments else []))
    for column in details.columns:
        key = Text()
        if column.primary_key:
            key.append("PK", style="bold yellow")
        if column.references:
            key.append(f"{' ' if key else ''}→ {column.references}", style="cyan")
        row = [
            Text(column.name, style="bold" if column.primary_key else ""),
            column.type,
            "yes" if column.nullable else "",
            column.default or "",
            key,
        ]
        if show_comments:
            row.append(column.comment or "")
        table.add_row(*row)
    return table


def _grid(*headers: str) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style="dim")
    for header in headers:
        table.add_column(header, overflow="fold")
    return table


def _section(title: str, body: RenderableType) -> list[RenderableType]:
    return [Text(""), Text(title, style="bold underline"), body]
