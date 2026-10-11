"""Read DuckDB's schema from its own catalog.

duckdb_engine reuses SQLAlchemy's PostgreSQL reflection, whose queries (since
SQLAlchemy 2.1) touch catalog tables and types DuckDB doesn't have, so columns,
keys and constraints can't be reflected. DuckDB's duckdb_columns() and
duckdb_constraints() have all of it.
"""

from __future__ import annotations

import re

from sqlalchemy import inspect, text
from sqlalchemy import types as sqltypes
from sqlalchemy.engine import Engine

SIMPLE_TYPES = {
    "BOOLEAN": sqltypes.Boolean,
    "TINYINT": sqltypes.SmallInteger,
    "SMALLINT": sqltypes.SmallInteger,
    "INTEGER": sqltypes.Integer,
    "BIGINT": sqltypes.BigInteger,
    "HUGEINT": sqltypes.BigInteger,
    "UTINYINT": sqltypes.SmallInteger,
    "USMALLINT": sqltypes.Integer,
    "UINTEGER": sqltypes.BigInteger,
    "UBIGINT": sqltypes.BigInteger,
    "FLOAT": sqltypes.Float,
    "REAL": sqltypes.Float,
    "DOUBLE": sqltypes.Float,
    "VARCHAR": sqltypes.String,
    "TEXT": sqltypes.Text,
    "BLOB": sqltypes.LargeBinary,
    "DATE": sqltypes.Date,
    "TIME": sqltypes.Time,
    "TIMESTAMP": sqltypes.DateTime,
    "INTERVAL": sqltypes.Interval,
    "UUID": sqltypes.Uuid,
    "JSON": sqltypes.JSON,
}


def column_type(name: str) -> sqltypes.TypeEngine:
    upper = name.upper()
    if upper.startswith("DECIMAL"):
        numbers = [int(n) for n in re.findall(r"\d+", upper)]
        return sqltypes.Numeric(*numbers[:2])
    if upper.startswith("TIMESTAMP"):
        return sqltypes.DateTime(timezone="TIME ZONE" in upper)
    return SIMPLE_TYPES.get(upper, sqltypes.String)()  # lists, structs, maps: shown by name, pasted as text


class DuckDBInspector:
    """The subset of SQLAlchemy's Inspector udbi uses, answered from DuckDB's catalog."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.inspector = inspect(engine)

    def __getattr__(self, name):  # table and schema listings work through duckdb_engine
        return getattr(self.inspector, name)

    def _rows(self, sql: str, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).mappings().all()

    def _constraints(self, table: str, schema: str | None, kind: str):
        return self._rows(
            "SELECT * FROM duckdb_constraints() WHERE schema_name = :schema AND table_name = :table "
            "AND database_name = current_database() AND constraint_type = :kind ORDER BY constraint_index",
            schema=schema or self.default_schema_name, table=table, kind=kind,
        )

    def get_schema_names(self, **kw):
        """This database's own schemas; duckdb_engine also lists the system and temp catalogs."""
        rows = self._rows(
            "SELECT schema_name FROM duckdb_schemas() WHERE database_name = current_database() "
            "AND schema_name NOT IN ('information_schema', 'pg_catalog') ORDER BY schema_name"
        )
        return [row["schema_name"] for row in rows]

    def get_columns(self, table: str, schema: str | None = None, **kw):
        rows = self._rows(
            "SELECT column_name, data_type, is_nullable, column_default, comment FROM duckdb_columns() "
            "WHERE schema_name = :schema AND table_name = :table AND database_name = current_database() "
            "ORDER BY column_index",
            schema=schema or self.default_schema_name, table=table,
        )
        return [
            {
                "name": row["column_name"],
                "type": column_type(row["data_type"]),
                "display_type": row["data_type"],
                "nullable": row["is_nullable"],
                "default": row["column_default"],
                "comment": row["comment"],
                "autoincrement": False,
            }
            for row in rows
        ]

    def get_pk_constraint(self, table: str, schema: str | None = None, **kw):
        rows = self._constraints(table, schema, "PRIMARY KEY")
        if not rows:
            return {"constrained_columns": [], "name": None}
        return {"constrained_columns": list(rows[0]["constraint_column_names"]), "name": rows[0]["constraint_name"]}

    def get_foreign_keys(self, table: str, schema: str | None = None, **kw):
        return [
            {
                "name": row["constraint_name"],
                "constrained_columns": list(row["constraint_column_names"]),
                "referred_schema": schema or self.default_schema_name,
                "referred_table": row["referenced_table"],
                "referred_columns": list(row["referenced_column_names"]),
            }
            for row in self._constraints(table, schema, "FOREIGN KEY")
        ]

    def get_multi_foreign_keys(self, schema: str | None = None, **kw):
        schema = schema or self.default_schema_name
        return {(schema, name): self.get_foreign_keys(name, schema) for name in self.inspector.get_table_names(schema=schema)}

    def get_unique_constraints(self, table: str, schema: str | None = None, **kw):
        return [
            {"name": row["constraint_name"], "column_names": list(row["constraint_column_names"])}
            for row in self._constraints(table, schema, "UNIQUE")
        ]

    def get_check_constraints(self, table: str, schema: str | None = None, **kw):
        return [
            {"name": row["constraint_name"], "sqltext": (row["expression"] or "").strip("()")}
            for row in self._constraints(table, schema, "CHECK")
        ]

    def get_indexes(self, table: str, schema: str | None = None, **kw):
        rows = self._rows(
            "SELECT index_name, is_unique, expressions FROM duckdb_indexes() WHERE schema_name = :schema "
            "AND table_name = :table AND database_name = current_database()",
            schema=schema or self.default_schema_name, table=table,
        )
        return [
            {"name": row["index_name"], "unique": row["is_unique"], "column_names": [row["expressions"].strip("[]'\"")]}
            for row in rows
        ]

    def get_table_comment(self, table: str, schema: str | None = None, **kw):
        rows = self._rows(
            "SELECT comment FROM duckdb_tables() WHERE schema_name = :schema AND table_name = :table "
            "AND database_name = current_database()",
            schema=schema or self.default_schema_name, table=table,
        )
        return {"text": rows[0]["comment"] if rows else None}
