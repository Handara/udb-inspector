import sqlite3

import pytest


@pytest.fixture
def sample_db(tmp_path):
    path = tmp_path / "sample.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            age INTEGER CHECK (age > 0)
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users (id),
            status VARCHAR(20) DEFAULT 'new'
        );
        CREATE INDEX orders_user_id ON orders (user_id);
        CREATE VIEW recent_orders AS SELECT * FROM orders;
        """
    )
    conn.close()
    return path


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Never read the real global config, a .udbi.toml above the repo, or $UDBI_URL."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("UDBI_URL", raising=False)
    monkeypatch.chdir(tmp_path)
