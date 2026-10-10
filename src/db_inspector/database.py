"""Connect to a detected target and ask the server what it is."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Connection, Dialect
from sqlalchemy.exc import DBAPIError, NoSuchModuleError

from .detect import Target


class ConnectError(Exception):
    """The database can't be reached, or its driver isn't installed."""


@dataclass(frozen=True)
class ServerInfo:
    product: str  # e.g. "PostgreSQL", "MariaDB", "CockroachDB"
    version: str
    banner: str | None  # first line of the server's own version string
    dialect: str
    driver: str
    default_schema: str | None


@dataclass(frozen=True)
class Relation:
    schema: str
    name: str
    kind: str  # "table" or "view"


DRIVER_EXTRAS = {
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "mssql": "mssql",
    "oracle": "oracle",
    "duckdb": "duckdb",
}

VERSION_QUERIES = {
    "postgresql": "SELECT version()",
    "mysql": "SELECT VERSION()",
    "mariadb": "SELECT VERSION()",
    "sqlite": "SELECT sqlite_version()",
    "mssql": "SELECT @@VERSION",
    "oracle": "SELECT banner FROM v$version WHERE ROWNUM = 1",
    "duckdb": "SELECT version()",
}

PRODUCT_NAMES = {
    "sqlite": "SQLite",
    "oracle": "Oracle",
    "duckdb": "DuckDB",
}

SYSTEM_SCHEMAS = {
    "postgresql": {"information_schema"},
    "mysql": {"information_schema", "mysql", "performance_schema", "sys"},
    "mariadb": {"information_schema", "mysql", "performance_schema", "sys"},
    "mssql": {"INFORMATION_SCHEMA", "sys", "guest"},
}

# Oracle reports every user as a schema; only the connected one is useful.
DEFAULT_SCHEMA_ONLY = {"oracle"}


class Database:
    def __init__(self, target: Target):
        self.target = target
        url = target.url
        if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
            # Connecting would silently create an empty file.
            if not Path(url.database).is_file():
                raise ConnectError(f"SQLite file {url.database} does not exist")
        try:
            self.engine = create_engine(url)
        except (ModuleNotFoundError, NoSuchModuleError) as exc:
            raise ConnectError(_missing_driver_message(url.get_backend_name(), exc)) from None

    def close(self) -> None:
        self.engine.dispose()

    def server_info(self) -> ServerInfo:
        try:
            with self.engine.connect() as conn:
                banner = _banner(conn)
                dialect = conn.dialect
                default_schema = dialect.default_schema_name
        except DBAPIError as exc:
            raise ConnectError(f"Can't connect to {self.target.display_url}: {exc.orig}") from None
        except ModuleNotFoundError as exc:  # some dialects import their driver lazily
            raise ConnectError(_missing_driver_message(dialect_name(self.engine.dialect), exc)) from None
        version_info = dialect.server_version_info or ()
        return ServerInfo(
            product=_product(dialect, banner),
            version=".".join(str(part) for part in version_info),
            banner=banner,
            dialect=dialect_name(dialect),
            driver=dialect.driver,
            default_schema=default_schema,
        )

    def schemas(self) -> list[str]:
        """User schemas, default schema first."""
        insp = inspect(self.engine)
        default = insp.default_schema_name
        name = dialect_name(self.engine.dialect)
        if name in DEFAULT_SCHEMA_ONLY:
            return [default] if default else []
        system = SYSTEM_SCHEMAS.get(name, set())
        schemas = [
            schema
            for schema in insp.get_schema_names()
            if schema not in system and not (name == "mssql" and schema.startswith("db_"))
        ]
        if default in schemas:
            schemas.remove(default)
            schemas.insert(0, default)
        return schemas

    def relations(self, schema: str) -> list[Relation]:
        """Tables then views in `schema`, each sorted by name."""
        insp = inspect(self.engine)
        tables = sorted(insp.get_table_names(schema=schema), key=str.lower)
        views = sorted(insp.get_view_names(schema=schema), key=str.lower)
        return [Relation(schema, name, "table") for name in tables] + [
            Relation(schema, name, "view") for name in views
        ]


def dialect_name(dialect: Dialect) -> str:
    # A mysql:// URL that reaches a MariaDB server still says "mysql".
    if dialect.name == "mysql" and getattr(dialect, "is_mariadb", False):
        return "mariadb"
    return dialect.name


def _banner(conn: Connection) -> str | None:
    sql = VERSION_QUERIES.get(conn.dialect.name)
    if sql is None:
        return None
    try:
        value = conn.exec_driver_sql(sql).scalar()
    except DBAPIError:  # e.g. no access to Oracle's v$version
        conn.rollback()
        return None
    if not value:
        return None
    return str(value).strip().splitlines()[0].strip()


def _product(dialect: Dialect, banner: str | None) -> str:
    text = (banner or "").lower()
    name = dialect_name(dialect)
    if name == "postgresql":
        # Postgres-compatible servers announce themselves in version().
        for marker, product in (
            ("cockroachdb", "CockroachDB"),
            ("redshift", "Amazon Redshift"),
            ("-yb-", "YugabyteDB"),
        ):
            if marker in text:
                return product
        return "PostgreSQL"
    if name in ("mysql", "mariadb"):
        if "tidb" in text:
            return "TiDB"
        return "MariaDB" if name == "mariadb" else "MySQL"
    if name == "mssql":
        return "Azure SQL" if "azure" in text else "SQL Server"
    return PRODUCT_NAMES.get(name, name)


def _missing_driver_message(backend: str, exc: Exception) -> str:
    extra = DRIVER_EXTRAS.get(backend)
    hint = f" Install it with: pip install 'db-inspector[{extra}]'" if extra else ""
    return f"The {backend} driver isn't installed ({exc}).{hint}"
