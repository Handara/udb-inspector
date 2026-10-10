import sqlite3

import pytest


@pytest.fixture
def sample_db(tmp_path):
    path = tmp_path / "sample.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users (id));
        CREATE VIEW recent_orders AS SELECT * FROM orders;
        """
    )
    conn.close()
    return path


@pytest.fixture(autouse=True)
def no_database_env(monkeypatch):
    for var in ("DBI_URL", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
