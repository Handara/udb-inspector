"""Connect to a detected target, ask the server what it is, and read its schema."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Connection, Dialect
from sqlalchemy.exc import DBAPIError, NoSuchModuleError, SAWarning
from sqlalchemy.types import TypeEngine

from .detect import Target

# Reflection warns about things it can't represent (expression indexes,
# extension types). We report what we can; printed warnings would garble the TUI.
warnings.filterwarnings(
    "ignore", message="(Skipped unsupported reflection|Did not recognize type)", category=SAWarning
)


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


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool
    default: str | None
    primary_key: bool
    references: str | None  # "users.id" when the column is part of a foreign key
    comment: str | None


@dataclass(frozen=True)
class ForeignKey:
    name: str | None
    schema: str
    table: str
    columns: tuple[str, ...]
    ref_schema: str
    ref_table: str
    ref_columns: tuple[str, ...]


@dataclass(frozen=True)
class Key:
    kind: str  # "primary key", "unique", "index", "unique index" or "check"
    name: str | None
    definition: str  # column list, or the expression of a check


@dataclass(frozen=True)
class TableDetails:
    relation: Relation
    columns: list[Column]
    keys: list[Key]
    foreign_keys: list[ForeignKey]
    comment: str | None
    view_definition: str | None


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
        # schema -> {(ref_schema, ref_table): foreign keys pointing at it}
        self._incoming_fks: dict[str, dict[tuple[str, str], list[ForeignKey]]] = {}

    def close(self) -> None:
        self.engine.dispose()

    def clear_cache(self) -> None:
        self._incoming_fks.clear()

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

    def describe(self, rel: Relation) -> TableDetails:
        insp = inspect(self.engine)
        name, schema = rel.name, rel.schema
        dialect = self.engine.dialect
        comment = (_optional(insp.get_table_comment, name, schema=schema) or {}).get("text")
        raw_columns = insp.get_columns(name, schema=schema)

        if rel.kind == "view":
            columns = [_column(raw, dialect, primary_key=False, references=None) for raw in raw_columns]
            definition = _optional(insp.get_view_definition, name, schema=schema)
            return TableDetails(rel, columns, [], [], comment, definition)

        pk = insp.get_pk_constraint(name, schema=schema)
        pk_columns = pk.get("constrained_columns") or []
        default_schema = insp.default_schema_name or schema
        foreign_keys = [
            _foreign_key(schema, name, raw, default_schema) for raw in insp.get_foreign_keys(name, schema=schema)
        ]
        references = {}
        for fk in foreign_keys:
            target = qualified(fk.ref_schema, fk.ref_table, schema)
            for column, ref_column in zip(fk.columns, fk.ref_columns):
                references[column] = f"{target}.{ref_column}"
        columns = [
            _column(raw, dialect, primary_key=raw["name"] in pk_columns, references=references.get(raw["name"]))
            for raw in raw_columns
        ]

        keys = []
        if pk_columns:
            keys.append(Key("primary key", pk.get("name"), ", ".join(pk_columns)))
        for uc in _optional(insp.get_unique_constraints, name, schema=schema) or []:
            keys.append(Key("unique", uc.get("name"), ", ".join(uc["column_names"])))
        for ix in insp.get_indexes(name, schema=schema):
            if ix.get("duplicates_constraint"):  # backs a unique constraint listed above
                continue
            elements = ix.get("expressions") or ix["column_names"]
            definition = ", ".join(element or "(expression)" for element in elements)
            keys.append(Key("unique index" if ix["unique"] else "index", ix.get("name"), definition))
        for check in _optional(insp.get_check_constraints, name, schema=schema) or []:
            keys.append(Key("check", check.get("name"), check["sqltext"]))

        return TableDetails(rel, columns, keys, foreign_keys, comment, None)

    def referenced_by(self, rel: Relation) -> list[ForeignKey]:
        """Foreign keys in the same schema that point at `rel`.

        Built once per schema: one query on PostgreSQL and Oracle, one per
        table elsewhere, so it is cached until clear_cache().
        """
        index = self._incoming_fks.get(rel.schema)
        if index is None:
            insp = inspect(self.engine)
            default_schema = insp.default_schema_name or rel.schema
            index = {}
            for (schema, table), raws in insp.get_multi_foreign_keys(schema=rel.schema).items():
                for raw in raws:
                    fk = _foreign_key(schema or rel.schema, table, raw, default_schema)
                    index.setdefault((fk.ref_schema, fk.ref_table), []).append(fk)
            self._incoming_fks[rel.schema] = index
        return index.get((rel.schema, rel.name), [])


def _optional(method, *args, **kwargs):
    """Call a reflection method that some dialects don't implement."""
    try:
        return method(*args, **kwargs)
    except NotImplementedError:
        return None


def _column(raw: dict, dialect: Dialect, *, primary_key: bool, references: str | None) -> Column:
    default = raw.get("default")
    return Column(
        name=raw["name"],
        type=_type_name(raw["type"], dialect),
        # SQLite reports INTEGER PRIMARY KEY as nullable; no primary key column is.
        nullable=bool(raw["nullable"]) and not primary_key,
        default=None if default is None else str(default),
        primary_key=primary_key,
        references=references,
        comment=raw.get("comment"),
    )


def _type_name(type_: TypeEngine, dialect: Dialect) -> str:
    try:
        return type_.compile(dialect=dialect)
    except Exception:  # NullType for types SQLAlchemy doesn't recognize
        return "unknown"


def _foreign_key(schema: str, table: str, raw: dict, default_schema: str) -> ForeignKey:
    return ForeignKey(
        name=raw.get("name"),
        schema=schema,
        table=table,
        columns=tuple(raw["constrained_columns"]),
        # Dialects leave referred_schema empty for tables on the default search path.
        ref_schema=raw.get("referred_schema") or default_schema,
        ref_table=raw["referred_table"],
        ref_columns=tuple(raw["referred_columns"]),
    )


def qualified(schema: str, table: str, home_schema: str) -> str:
    return table if schema == home_schema else f"{schema}.{table}"


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
