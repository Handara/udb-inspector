from __future__ import annotations

import argparse

from rich.console import Console
from rich.text import Text

from .database import ConnectError, Database
from .detect import DetectionError, resolve
from .render import connection_table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dbi", description="Inspect a database from your terminal.")
    parser.add_argument(
        "target",
        nargs="?",
        help="database URL or SQLite/DuckDB file (default: $DBI_URL, then $DATABASE_URL)",
    )
    parser.add_argument(
        "--info", action="store_true", help="print the detected database and exit instead of opening the inspector"
    )
    args = parser.parse_args(argv)

    try:
        target = resolve(args.target)
        db = Database(target)
        info = db.server_info()
    except (DetectionError, ConnectError) as exc:
        Console(stderr=True).print(Text.assemble(("error: ", "bold red"), str(exc)))
        return 1

    try:
        if args.info:
            Console().print(connection_table(target, info))
        else:
            from .tui.app import InspectorApp  # Textual is only needed for the UI

            InspectorApp(db, info).run()
    finally:
        db.close()
    return 0
