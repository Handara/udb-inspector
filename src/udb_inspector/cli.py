from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.text import Text

from .connections import NothingToOpen, resolve
from .database import ConnectError, Database
from .detect import DetectionError
from .render import connection_table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="udbi",
        description="Inspect a database from your terminal. Connections are set up inside the app.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="database URL, SQLite/DuckDB file, or saved connection name "
        "(default: $UDBI_URL, then this project's default connection, then the global default)",
    )
    parser.add_argument(
        "-g", "--global", dest="global_only", action="store_true", help="use the global connections, ignoring the project"
    )
    parser.add_argument(
        "--info", action="store_true", help="print the connection details and exit instead of opening the app"
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.info:
        return info(args.target, args.global_only)

    # The app opens no matter what; anything that went wrong is shown inside it.
    target, error = None, None
    try:
        target = resolve(args.target, global_only=args.global_only)
    except NothingToOpen:
        pass
    except DetectionError as exc:
        error = str(exc)

    from .tui.app import InspectorApp  # Textual is only needed for the UI

    InspectorApp(target, error).run()
    return 0


def info(name: str | None, global_only: bool) -> int:
    try:
        db = Database(resolve(name, global_only=global_only))
        server = db.server_info()
    except (DetectionError, ConnectError) as exc:
        Console(stderr=True).print(Text.assemble(("error: ", "bold red"), str(exc)))
        return 1
    try:
        Console().print(connection_table(db.target, server))
    finally:
        db.close()
    return 0
