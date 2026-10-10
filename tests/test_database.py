import sqlite3

import pytest

from db_inspector import database
from db_inspector.cli import main
from db_inspector.database import ConnectError, Database, Relation
from db_inspector.detect import resolve


def test_server_info_for_sqlite(sample_db):
    info = Database(resolve(str(sample_db))).server_info()
    assert info.product == "SQLite"
    assert info.version == sqlite3.sqlite_version
    assert info.dialect == "sqlite"
    assert info.default_schema == "main"


def test_relations_lists_tables_then_views(sample_db):
    db = Database(resolve(str(sample_db)))
    assert db.schemas() == ["main"]
    assert db.relations("main") == [
        Relation("main", "orders", "table"),
        Relation("main", "users", "table"),
        Relation("main", "recent_orders", "view"),
    ]


def test_sqlite_url_to_missing_file_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(ConnectError, match="does not exist"):
        Database(resolve(f"sqlite:///{path}"))
    assert not path.exists()


def test_missing_driver_names_the_extra(monkeypatch):
    def fail(url):
        raise ModuleNotFoundError("No module named 'psycopg'")

    monkeypatch.setattr(database, "create_engine", fail)
    with pytest.raises(ConnectError, match=r"pip install 'db-inspector\[postgres\]'"):
        Database(resolve("postgres://localhost/app"))


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
    assert main([str(tmp_path / "nope.db")]) == 1
    assert "no such file" in capsys.readouterr().err
