"""Work out which database to open and how to reach it.

A target is a database URL or a SQLite/DuckDB file. With no target we fall
back to $DBI_URL, then $DATABASE_URL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


class DetectionError(Exception):
    """The target can't be turned into a database URL."""


@dataclass(frozen=True)
class Target:
    url: URL
    source: str  # where the URL came from, shown to the user

    @property
    def display_url(self) -> str:
        return self.url.render_as_string(hide_password=True)


# Bare schemes get the driver we ship as an install extra, so
# postgres://... works without knowing SQLAlchemy's driver names.
DEFAULT_DRIVERS = {
    "postgres": "postgresql+psycopg",
    "postgresql": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
    "mariadb": "mariadb+pymysql",
    "mssql": "mssql+pyodbc",
    "sqlserver": "mssql+pyodbc",
    "oracle": "oracle+oracledb",
}

SQLITE_MAGIC = b"SQLite format 3\x00"
SQLITE_SUFFIXES = {".db", ".db3", ".sqlite", ".sqlite3"}
DUCKDB_MAGIC = b"DUCK"  # at byte offset 8


def resolve(target: str | None) -> Target:
    if target:
        if "://" in target:
            return Target(parse_url(target), "URL")
        return from_file(Path(target).expanduser())
    for var in ("DBI_URL", "DATABASE_URL"):
        if value := os.environ.get(var):
            return Target(parse_url(value), f"${var}")
    raise DetectionError("No database given. Pass a URL or file path, or set $DBI_URL.")


def parse_url(raw: str) -> URL:
    try:
        url = make_url(raw)
    except ArgumentError:
        raise DetectionError("Can't parse the database URL") from None
    if "+" not in url.drivername and url.drivername in DEFAULT_DRIVERS:
        url = url.set(drivername=DEFAULT_DRIVERS[url.drivername])
    return url


def from_file(path: Path) -> Target:
    if not path.is_file():
        raise DetectionError(f"{path}: no such file, and not a database URL")
    with path.open("rb") as fh:
        header = fh.read(16)
    absolute = str(path.resolve())
    # SQLite treats an empty file as an empty database.
    if header.startswith(SQLITE_MAGIC) or (not header and path.suffix.lower() in SQLITE_SUFFIXES):
        return Target(URL.create("sqlite", database=absolute), "SQLite file header")
    if header[8:12] == DUCKDB_MAGIC:
        return Target(URL.create("duckdb", database=absolute), "DuckDB file header")
    raise DetectionError(f"{path} is not a SQLite or DuckDB database file")
