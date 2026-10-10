"""Rich renderables shared by the CLI and the TUI."""

from __future__ import annotations

from rich.table import Table

from .database import ServerInfo
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
