import sqlite3

import pytest

from udb_inspector import database
from udb_inspector.cli import main
from udb_inspector.database import ConnectError, Database, Key, Relation
from udb_inspector.detect import detect


def test_server_info_for_sqlite(sample_db):
    info = Database(detect(str(sample_db))).server_info()
    assert info.product == "SQLite"
    assert info.version == sqlite3.sqlite_version
    assert info.dialect == "sqlite"
    assert info.default_schema == "main"


def test_relations_lists_tables_then_views(sample_db):
    db = Database(detect(str(sample_db)))
    assert db.schemas() == ["main"]
    assert db.relations("main") == [
        Relation("main", "orders", "table"),
        Relation("main", "users", "table"),
        Relation("main", "recent_orders", "view"),
    ]


def test_sqlite_url_to_missing_file_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(ConnectError, match="does not exist"):
        Database(detect(f"sqlite:///{path}"))
    assert not path.exists()


def test_missing_driver_names_the_extra(monkeypatch):
    def fail(url):
        raise ModuleNotFoundError("No module named 'psycopg'")

    monkeypatch.setattr(database, "create_engine", fail)
    with pytest.raises(ConnectError, match=r"pip install 'udb-inspector\[postgres\]'"):
        Database(detect("postgres://localhost/app"))


@pytest.mark.parametrize(
    ("banner", "product"),
    [
        ("PostgreSQL 16.4 on x86_64-pc-linux-gnu", "PostgreSQL"),
        ("CockroachDB CCL v24.1.0 (x86_64-pc-linux-gnu)", "CockroachDB"),
        ("PostgreSQL 8.0.2 on i686-pc-linux-gnu, Redshift 1.0.77467", "Amazon Redshift"),
        ("PostgreSQL 11.2-YB-2.20.0.0-b0 on x86_64", "YugabyteDB"),
    ],
)
def test_postgres_compatible_products(banner, product):
    from sqlalchemy.dialects import postgresql

    assert database._product(postgresql.dialect(), banner) == product


def test_cli_info(sample_db, capsys):
    assert main([str(sample_db), "--info"]) == 0
    out = capsys.readouterr().out
    assert "SQLite" in out and "sample.sqlite3" in out


def test_cli_error_goes_to_stderr(tmp_path, capsys):
    assert main([str(tmp_path / "nope.db"), "--info"]) == 1
    assert "no such file" in capsys.readouterr().err


def test_describe_table(sample_db):
    db = Database(detect(str(sample_db)))
    details = db.describe(Relation("main", "orders", "table"))
    columns = {column.name: column for column in details.columns}
    assert list(columns) == ["id", "user_id", "status"]
    assert columns["id"].primary_key and not columns["id"].nullable
    assert columns["user_id"].references == "users.id"
    assert not columns["user_id"].nullable
    assert columns["status"].type == "VARCHAR(20)"
    assert columns["status"].default == "'new'"
    assert Key("primary key", None, "id") in details.keys
    assert Key("index", "orders_user_id", "user_id") in details.keys
    assert [(fk.columns, fk.ref_table, fk.ref_columns) for fk in details.foreign_keys] == [
        (("user_id",), "users", ("id",))
    ]


def test_describe_unique_and_check(sample_db):
    db = Database(detect(str(sample_db)))
    keys = db.describe(Relation("main", "users", "table")).keys
    assert Key("unique", None, "email") in keys
    assert Key("check", None, "age > 0") in keys


def test_describe_view(sample_db):
    db = Database(detect(str(sample_db)))
    details = db.describe(Relation("main", "recent_orders", "view"))
    assert [column.name for column in details.columns] == ["id", "user_id", "status"]
    assert "SELECT * FROM orders" in details.view_definition
    assert details.keys == [] and details.foreign_keys == []


def test_referenced_by(sample_db):
    db = Database(detect(str(sample_db)))
    incoming = db.referenced_by(Relation("main", "users", "table"))
    assert [(fk.table, fk.columns) for fk in incoming] == [("orders", ("user_id",))]
    assert db.referenced_by(Relation("main", "orders", "table")) == []
