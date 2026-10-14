"""Connect to a detected target, ask the server what it is, and read its schema."""

from __future__ import annotations

import re
import sys
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Column as SAColumn
from sqlalchemy import MetaData, Table, and_, create_engine, delete, insert, inspect, select, text, update
from sqlalchemy.engine import Connection, Dialect
from sqlalchemy.exc import DBAPIError, NoSuchModuleError, SAWarning
from sqlalchemy.types import TypeEngine

from .detect import SALESFORCE, Target
from .sql import check_read_only, count_query, paged_query, single_query, sqlglot_dialect

# Reflection warns about things it can't represent (expression indexes,
# extension types). We report what we can; printed warnings would garble the TUI.
warnings.filterwarnings(
    "ignore", message="(Skipped unsupported reflection|Did not recognize type)", category=SAWarning
)


class ConnectError(Exception):
    """The database can't be reached, or its driver isn't installed."""


class QueryError(Exception):
    """The database rejected a query."""


class WritesNotAllowed(Exception):
    """The connection isn't marked allow_writes."""


class NoMatchingRow(Exception):
    """An update or delete found no row with the given key."""


@dataclass
class RowFailure:
    line: int  # the spreadsheet row
    cells: list[str]
    error: str


@dataclass
class ImportReport:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    failures: list[RowFailure] = field(default_factory=list)
    stopped: str | None = None  # why the import gave up early, if it did

    @property
    def written(self) -> int:
        return self.inserted + self.updated + self.deleted


AUTH_ERROR = re.compile(r"password|authentication|access denied|login failed|ORA-01017", re.IGNORECASE)


def is_auth_error(exc: Exception) -> bool:
    """Whether the server turned down the login (as opposed to being unreachable)."""
    return bool(AUTH_ERROR.search(str(exc)))


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


@dataclass(frozen=True)
class QueryResult:
    sql: str
    columns: list[str]
    rows: list[tuple]
    truncated: bool  # the query returned more rows than were fetched
    elapsed: float  # seconds


@dataclass(frozen=True)
class Page:
    columns: list[str]
    rows: list[tuple]
    has_more: bool  # rows exist after this page
    elapsed: float  # seconds
    sql: str = ""  # what was sent: the query with paging (and any sort and search) added


MAX_ROWS = 10_000

# Where one failed statement spoils the whole transaction (PostgreSQL) or might,
# each imported row gets a savepoint. SQLite only undoes the failed statement.
# DuckDB has no savepoints, so its rows are committed one at a time.
SAVEPOINT_DIALECTS = {"postgresql", "mysql", "mariadb", "mssql", "oracle"}
COMMIT_EACH_ROW = {"duckdb"}
GIVE_UP_AFTER = 10  # failures in a row, before anything succeeded

# Statements that make the transaction (or, for SQLite, the connection)
# refuse writes. SQL Server and DuckDB have none; the read-only check and the
# rollback cover them, and their DDL is transactional.
READ_ONLY_SETUP = {
    "postgresql": "SET TRANSACTION READ ONLY",
    "mysql": "SET TRANSACTION READ ONLY",
    "mariadb": "SET TRANSACTION READ ONLY",
    "oracle": "SET TRANSACTION READ ONLY",
    "sqlite": "PRAGMA query_only = ON",
}

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


def open_database(target: Target):
    """The backend for a target: a Salesforce org, or a database SQLAlchemy reaches."""
    if target.url.drivername == SALESFORCE:
        from .salesforce import SalesforceOrg

        return SalesforceOrg(target)
    return Database(target)


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
        self._columns: dict[Relation, list[tuple[str, str]]] = {}

    def close(self) -> None:
        self.engine.dispose()

    def _inspect(self):
        """SQLAlchemy's inspector, or for DuckDB one that reads DuckDB's own catalog."""
        if dialect_name(self.engine.dialect) == "duckdb":
            from .duckdb_schema import DuckDBInspector

            return DuckDBInspector(self.engine)
        return inspect(self.engine)

    def clear_cache(self) -> None:
        self._incoming_fks.clear()
        self._columns.clear()

    def column_names(self, rel: Relation) -> list[tuple[str, str]]:
        """(name, type) of each column; cheaper than describe(), for completions."""
        if rel not in self._columns:
            raw = self._inspect().get_columns(rel.name, schema=rel.schema)
            self._columns[rel] = [(column["name"], column.get("display_type") or _type_name(column["type"], self.engine.dialect)) for column in raw]
        return self._columns[rel]

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
        version = ".".join(str(part) for part in dialect.server_version_info or ())
        if dialect.name == "duckdb" and banner:
            # duckdb_engine reports its Postgres-compatibility version; the real one is in version().
            version, banner = banner.removeprefix("v"), None
        return ServerInfo(
            product=_product(dialect, banner),
            version=version,
            banner=banner,
            dialect=dialect_name(dialect),
            driver=dialect.driver,
            default_schema=default_schema,
        )

    def schemas(self) -> list[str]:
        """User schemas, default schema first."""
        insp = self._inspect()
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
        insp = self._inspect()
        tables = sorted(insp.get_table_names(schema=schema), key=str.lower)
        views = sorted(insp.get_view_names(schema=schema), key=str.lower)
        return [Relation(schema, name, "table") for name in tables] + [
            Relation(schema, name, "view") for name in views
        ]

    def describe(self, rel: Relation) -> TableDetails:
        insp = self._inspect()
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
        if dialect_name(dialect) == "sqlite":
            # SQLAlchemy misses inline UNIQUE after a sized type (name VARCHAR(150) UNIQUE);
            # SQLite's own catalog has every one.
            listed = {key.definition for key in keys if key.kind == "unique"}
            keys += [Key("unique", None, columns) for columns in self._sqlite_unique(rel) if columns not in listed]
        for ix in insp.get_indexes(name, schema=schema):
            if ix.get("duplicates_constraint"):  # backs a unique constraint listed above
                continue
            elements = ix.get("expressions") or ix["column_names"]
            definition = ", ".join(element or "(expression)" for element in elements)
            keys.append(Key("unique index" if ix["unique"] else "index", ix.get("name"), definition))
        for check in _optional(insp.get_check_constraints, name, schema=schema) or []:
            keys.append(Key("check", check.get("name"), check["sqltext"]))

        return TableDetails(rel, columns, keys, foreign_keys, comment, None)

    def quoted_name(self, rel: Relation) -> str:
        """The relation's name as SQL, quoted where needed and schema-qualified outside the default schema."""
        preparer = self.engine.dialect.identifier_preparer
        name = preparer.quote(rel.name)
        if rel.schema and rel.schema != self.engine.dialect.default_schema_name:
            return f"{preparer.quote_schema(rel.schema)}.{name}"
        return name

    def forget(self, sql: str) -> None:
        """Nothing to forget: every page is a query of its own. (Salesforce keeps cursors.)"""

    def quote_identifier(self, name: str) -> str:
        return self.engine.dialect.identifier_preparer.quote(name)

    def browse_sql(self, rel: Relation) -> str:
        """The query that shows a table's rows."""
        return f"SELECT * FROM {self.quoted_name(rel)}"

    def run_query(self, sql: str, max_rows: int = MAX_ROWS) -> QueryResult:
        """Run a read-only query, fetching at most `max_rows` rows."""
        dialect = dialect_name(self.engine.dialect)
        check_read_only(sql, dialect)
        options = {"no_parameters": True}  # send the SQL as typed: no % placeholders for psycopg and pymysql
        if dialect == "postgresql" and single_query(sql, sqlglot_dialect(dialect)) is not None:
            # psycopg otherwise downloads the whole result before the first row;
            # a server-side cursor stops at max_rows.
            options["stream_results"] = True
        started = time.perf_counter()
        with self._read_only() as conn:
            try:
                result = conn.execution_options(**options).exec_driver_sql(sql)
                if not result.returns_rows:
                    return QueryResult(sql, [], [], False, time.perf_counter() - started)
                columns = list(result.keys())
                rows = result.fetchmany(max_rows + 1)
            except DBAPIError as exc:
                raise QueryError(str(exc.orig).strip()) from None
        return QueryResult(
            sql, columns, [tuple(row) for row in rows[:max_rows]], len(rows) > max_rows, time.perf_counter() - started
        )

    def import_columns(self, rel: Relation):
        """What the importer needs to know about each column."""
        from .importing import ColumnInfo

        insp = self._inspect()
        raw_columns = insp.get_columns(rel.name, schema=rel.schema)
        pk = set(insp.get_pk_constraint(rel.name, schema=rel.schema).get("constrained_columns") or [])
        integer_pk = len(pk) == 1
        columns = []
        for raw in raw_columns:
            default = str(raw.get("default") or "")
            generated = (
                raw.get("autoincrement") is True
                or bool(raw.get("identity"))
                or "nextval(" in default
                or (integer_pk and raw["name"] in pk and self.engine.dialect.name == "sqlite" and _type_name(raw["type"], self.engine.dialect).upper() == "INTEGER")
            )
            columns.append(
                ColumnInfo(
                    name=raw["name"],
                    type=raw["type"],
                    type_name=raw.get("display_type") or _type_name(raw["type"], self.engine.dialect),
                    nullable=bool(raw["nullable"]) and raw["name"] not in pk,
                    has_default=generated or raw.get("default") is not None,
                    primary_key=raw["name"] in pk,
                    generated=generated,
                )
            )
        return columns

    def write_rows(self, rel: Relation, operation: str, key: list[str], rows, progress=None) -> ImportReport:
        """Insert, update, upsert or delete planned rows one by one, skipping the ones that fail."""
        if not self.target.allow_writes:
            raise WritesNotAllowed("This connection doesn't allow writes.")
        table = Table(rel.name, MetaData(), *[SAColumn(c.name, c.type) for c in self.import_columns(rel)], schema=rel.schema)
        dialect = dialect_name(self.engine.dialect)
        report = ImportReport()
        failed_in_a_row = 0
        with self.engine.connect() as conn:
            for done, row in enumerate(rows, start=1):
                try:
                    if dialect in SAVEPOINT_DIALECTS:
                        with conn.begin_nested():
                            outcome = _write_row(conn, table, operation, key, row.values)
                    else:
                        outcome = _write_row(conn, table, operation, key, row.values)
                        if dialect in COMMIT_EACH_ROW:
                            conn.commit()
                except (DBAPIError, NoMatchingRow) as exc:
                    if dialect in COMMIT_EACH_ROW:
                        conn.rollback()
                    error = str(exc.orig).strip() if isinstance(exc, DBAPIError) else str(exc)
                    report.failures.append(RowFailure(row.line, row.cells, error.splitlines()[0]))
                    failed_in_a_row += 1
                    if failed_in_a_row >= GIVE_UP_AFTER and report.written == 0:
                        report.stopped = f"Stopped after the first {GIVE_UP_AFTER} rows all failed: {report.failures[-1].error}"
                        break
                else:
                    setattr(report, outcome, getattr(report, outcome) + 1)
                    failed_in_a_row = 0
                if progress is not None:
                    progress(done)
            conn.commit()
        return report

    def fetch_row(self, rel: Relation, where: dict[str, object]) -> tuple[list[str], tuple] | None:
        """The first row of `rel` whose columns equal `where`, as (columns, values)."""
        with self._read_only() as conn:
            try:
                result = conn.execute(self._where(f"SELECT * FROM {self.quoted_name(rel)}", where))
                row = result.fetchone()
            except DBAPIError as exc:
                raise QueryError(str(exc.orig).strip()) from None
            return (list(result.keys()), tuple(row)) if row is not None else None

    def count_rows(self, rel: Relation, where: dict[str, object]) -> int:
        with self._read_only() as conn:
            try:
                return conn.execute(self._where(f"SELECT COUNT(*) FROM {self.quoted_name(rel)}", where)).scalar_one()
            except DBAPIError as exc:
                raise QueryError(str(exc.orig).strip()) from None

    def count_each(self, queries: list[tuple[Relation, dict[str, object]]]) -> Iterator[tuple[int, int]]:
        """count_rows for each (relation, where), as (index, count) when it's known; failures are left out."""
        for index, (rel, where) in enumerate(queries):
            try:
                yield index, self.count_rows(rel, where)
            except QueryError:
                continue

    def target_of(self, fk: ForeignKey, value: object) -> Relation:
        """The table a foreign key's value points into."""
        return Relation(fk.ref_schema, fk.ref_table, "table")

    def select_sql(self, rel: Relation, where: dict[str, object]) -> str:
        """SELECT * … WHERE col = value with the values written out, for the query editor."""
        statement = self._where(f"SELECT * FROM {self.quoted_name(rel)}", where)
        return str(statement.compile(dialect=self.engine.dialect, compile_kwargs={"literal_binds": True}))

    def _where(self, select: str, where: dict[str, object]):
        quote = self.engine.dialect.identifier_preparer.quote
        conditions = " AND ".join(f"{quote(column)} = :p{index}" for index, column in enumerate(where))
        params = {f"p{index}": value for index, value in enumerate(where.values())}
        return text(f"{select} WHERE {conditions}").bindparams(**params)

    def _as_stored(self, columns: list[str], sort: tuple[str, bool] | None) -> tuple[list[str], tuple[str, bool] | None]:
        """Column names spelled as the database stores them, for the quoted names in
        paging SQL. SQLAlchemy reports Oracle's TOTAL as total, and "total" is another column."""
        dialect = self.engine.dialect
        if not dialect.requires_name_normalize:
            return list(columns), sort
        return [dialect.denormalize_name(name) for name in columns], sort and (dialect.denormalize_name(sort[0]), sort[1])

    def fetch_page(
        self, sql: str, *, offset: int, limit: int, sort: tuple[str, bool] | None = None, search: str = "", columns: list[str] = ()
    ) -> Page | None:
        """One page of a query, sorted and searched by the database. None when the
        query can't be paged (it isn't a single SELECT sqlglot can rewrite)."""
        dialect = dialect_name(self.engine.dialect)
        columns, sort = self._as_stored(columns, sort)
        paged = paged_query(
            sql, sqlglot_dialect(dialect), columns=columns, sort=sort, search=search, offset=offset, limit=limit + 1
        )
        if paged is None:
            return None
        check_read_only(sql, dialect)
        started = time.perf_counter()
        with self._read_only() as conn:
            try:
                result = conn.execution_options(no_parameters=True).exec_driver_sql(paged)
                columns, rows = list(result.keys()), result.fetchall()
            except DBAPIError as exc:
                raise QueryError(str(exc.orig).strip()) from None
        return Page(columns, [tuple(row) for row in rows[:limit]], len(rows) > limit, time.perf_counter() - started, paged)

    def count_matching(self, sql: str, *, search: str = "", columns: list[str] = ()) -> int | None:
        """How many rows the query returns (with the search applied), or None if it can't be counted."""
        dialect = dialect_name(self.engine.dialect)
        columns, _ = self._as_stored(columns, None)
        counting = count_query(sql, sqlglot_dialect(dialect), columns=columns, search=search)
        if counting is None:
            return None
        check_read_only(sql, dialect)
        with self._read_only() as conn:
            try:
                return int(conn.execution_options(no_parameters=True).exec_driver_sql(counting).scalar_one())
            except DBAPIError as exc:
                raise QueryError(str(exc.orig).strip()) from None

    def filtered_sql(self, sql: str, *, sort: tuple[str, bool] | None, search: str, columns: list[str]) -> str:
        """The query with the search and sort applied but no page limit, for exporting what's shown."""
        dialect = sqlglot_dialect(dialect_name(self.engine.dialect))
        columns, sort = self._as_stored(columns, sort)
        return paged_query(sql, dialect, columns=columns, sort=sort, search=search) or sql

    @contextmanager
    def stream_query(self, sql: str, batch_size: int = 1000) -> Iterator[tuple[list[str], Iterator[list[tuple]]]]:
        """Run a read-only query for export: (columns, batches of rows), all of them.

        stream_results asks for a server-side cursor where the driver has one,
        so a big result isn't held in memory.
        """
        check_read_only(sql, dialect_name(self.engine.dialect))
        with self._read_only() as conn:
            try:
                result = conn.execution_options(no_parameters=True, stream_results=True).exec_driver_sql(sql)
            except DBAPIError as exc:
                raise QueryError(str(exc.orig).strip()) from None

            def batches() -> Iterator[list[tuple]]:
                while True:
                    try:
                        rows = result.fetchmany(batch_size)
                    except DBAPIError as exc:
                        raise QueryError(str(exc.orig).strip()) from None
                    if not rows:
                        return
                    yield [tuple(row) for row in rows]

            yield (list(result.keys()), batches()) if result.returns_rows else ([], iter(()))

    @contextmanager
    def _read_only(self) -> Iterator[Connection]:
        """A connection in a read-only transaction that is always rolled back."""
        with self.engine.connect() as conn:
            setup = READ_ONLY_SETUP.get(dialect_name(conn.dialect))
            if setup:
                try:
                    conn.exec_driver_sql(setup)
                except DBAPIError:  # e.g. a Postgres-compatible server without it
                    conn.rollback()
            try:
                yield conn
            finally:
                conn.rollback()

    def _sqlite_unique(self, rel: Relation) -> list[str]:
        quote = self.engine.dialect.identifier_preparer.quote
        prefix = f"{quote(rel.schema)}." if rel.schema else ""
        with self.engine.connect() as conn:
            indexes = conn.exec_driver_sql(f"PRAGMA {prefix}index_list({quote(rel.name)})").fetchall()
            found = []
            for _seq, index, _unique, origin, *_ in indexes:
                if origin == "u":  # made by a UNIQUE constraint, not CREATE INDEX
                    info = conn.exec_driver_sql(f"PRAGMA {prefix}index_info({quote(index)})").fetchall()
                    found.append(", ".join(row[2] for row in info))
            return found

    def referenced_by(self, rel: Relation) -> list[ForeignKey]:
        """Foreign keys in the same schema that point at `rel`.

        Built once per schema: one query on PostgreSQL and Oracle, one per
        table elsewhere, so it is cached until clear_cache().
        """
        index = self._incoming_fks.get(rel.schema)
        if index is None:
            insp = self._inspect()
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
        type=raw.get("display_type") or _type_name(raw["type"], dialect),
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
    if getattr(sys, "frozen", False):  # the standalone executable can't install anything
        hint = " SQL Server needs the Python version of udbi plus Microsoft's ODBC driver." if backend == "mssql" else ""
        return f"This udbi build doesn't include the {backend} driver ({exc}).{hint}"
    extra = DRIVER_EXTRAS.get(backend)
    hint = f" Install it with: pip install 'udb-inspector[{extra}]'" if extra else ""
    return f"The {backend} driver isn't installed ({exc}).{hint}"


def _write_row(conn: Connection, table: Table, operation: str, key: list[str], values: dict[str, object]) -> str:
    """One row; returns which counter it adds to."""
    if operation == "insert":
        conn.execute(insert(table).values(**values))
        return "inserted"
    match = and_(*(table.c[column] == values[column] for column in key))
    described = ", ".join(f"{column} = {values[column]!r}" for column in key)
    if operation == "delete":
        if conn.execute(delete(table).where(match)).rowcount == 0:
            raise NoMatchingRow(f"No row with {described}.")
        return "deleted"
    changes = {column: value for column, value in values.items() if column not in key}
    if changes and conn.execute(update(table).where(match).values(**changes)).rowcount > 0:
        return "updated"
    if operation == "update":
        if changes:
            raise NoMatchingRow(f"No row with {described}.")
        return "updated"
    # Upsert: the update matched nothing (or there was nothing to change).
    if not changes and conn.execute(select(1).select_from(table).where(match)).first() is not None:
        return "updated"
    conn.execute(insert(table).values(**values))
    return "inserted"
