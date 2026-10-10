from __future__ import annotations

import argparse
import re
import sys
from getpass import getpass
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.text import Text

from .connections import resolve
from .database import ConnectError, Database, ServerInfo
from .detect import DetectionError, Target
from .render import connection_table
from .wizard import Prompter, run_init

AUTH_ERROR = re.compile(r"password|authentication|access denied|login failed|ORA-01017", re.IGNORECASE)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["init"]:
        return init(argv[1:])

    parser = argparse.ArgumentParser(
        prog="dbi",
        description="Inspect a database from your terminal.",
        epilog="Run `dbi init` to save a connection for this project, or `dbi init -g` for a global one.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="database URL, SQLite/DuckDB file, or saved connection name "
        "(default: $DBI_URL, then this project's default connection, then the global default)",
    )
    parser.add_argument(
        "-g", "--global", dest="global_only", action="store_true", help="use the global connections, ignoring the project"
    )
    parser.add_argument(
        "--info", action="store_true", help="print the detected database and exit instead of opening the inspector"
    )
    args = parser.parse_args(argv)

    try:
        db, info = connect(resolve(args.target, global_only=args.global_only))
    except (DetectionError, ConnectError) as exc:
        _error(exc)
        return 1
    except (KeyboardInterrupt, EOFError):  # gave up at the password prompt
        print(file=sys.stderr)
        return 130

    try:
        if args.info:
            Console().print(connection_table(db.target, info))
        else:
            from .tui.app import InspectorApp  # Textual is only needed for the UI

            InspectorApp(db, info).run()
    finally:
        db.close()
    return 0


def connect(target: Target) -> tuple[Database, ServerInfo]:
    """Connect, asking for a password if the server wants one we don't have."""
    db = Database(target)
    try:
        return db, db.server_info()
    except ConnectError as exc:
        db.close()
        if target.url.password is not None or not AUTH_ERROR.search(str(exc)) or not sys.stdin.isatty():
            raise
    url = target.url
    password = getpass(f"Password for {url.username or 'user'}@{url.host or 'localhost'}: ")
    db = Database(target.with_password(password))
    try:
        return db, db.server_info()
    except ConnectError:
        db.close()
        raise


def init(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="dbi init",
        description="Save a named connection in this project's .dbi.toml, or in the global config with -g.",
    )
    parser.add_argument("-g", "--global", dest="global_only", action="store_true", help="add a global connection")
    args = parser.parse_args(argv)

    console = Console()
    io = Prompter(
        ask=lambda question, default: Prompt.ask(question, console=console, default=default) if default else Prompt.ask(question, console=console),
        confirm=lambda question, default: Confirm.ask(question, console=console, default=default),
        secret=lambda question: getpass(f"{question}: "),
        say=lambda text: console.print(text, markup=False, highlight=False),
    )
    try:
        saved = run_init("global" if args.global_only else "project", Path.cwd(), io, check=_describe)
    except (DetectionError, ConnectError) as exc:
        _error(exc)
        return 1
    except (KeyboardInterrupt, EOFError):
        console.print()
        return 130
    return 0 if saved else 1


def _describe(target: Target) -> str:
    db = Database(target)
    try:
        info = db.server_info()
    finally:
        db.close()
    return f"{info.product} {info.version}".strip()


def _error(exc: Exception) -> None:
    Console(stderr=True).print(Text.assemble(("error: ", "bold red"), str(exc)))
