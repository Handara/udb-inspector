"""Against real servers: set UDBI_TEST_POSTGRES and/or UDBI_TEST_MYSQL to a database URL.

GitHub Actions starts both (see .github/workflows/tests.yml); locally these skip.
"""

import os
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, text

from udb_inspector.database import Database, Relation
from udb_inspector.detect import detect
from udb_inspector.importing import parse_pasted, plan
from udb_inspector.sql import ReadOnlyError

SERVERS = {"postgresql": "UDBI_TEST_POSTGRES", "mysql": "UDBI_TEST_MYSQL"}
CONFIGURED = [pytest.param(name, id=name) for name, variable in SERVERS.items() if os.environ.get(variable)]

SCHEMA = {
    "postgresql": [
        "CREATE TABLE users (id SERIAL PRIMARY KEY, email VARCHAR(100) NOT NULL UNIQUE, score NUMERIC(10, 2), joined DATE)",
        "CREATE TABLE orders (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users (id), note VARCHAR(50))",
    ],
    "mysql": [
        "CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, email VARCHAR(100) NOT NULL UNIQUE, score DECIMAL(10, 2), joined DATE)",
        "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, user_id INT, note VARCHAR(50), "
        "FOREIGN KEY (user_id) REFERENCES users (id))",
    ],
}

@pytest.fixture(params=CONFIGURED or [pytest.param(None, marks=pytest.mark.skip(reason="no live database configured"))])
def server(request):
    url = os.environ[SERVERS[request.param]]
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS orders"))
        conn.execute(text("DROP TABLE IF EXISTS users"))
        for statement in SCHEMA[request.param]:
            conn.execute(text(statement))
        conn.execute(text("INSERT INTO users (email, score, joined) VALUES ('a@x', 1.50, '2026-01-01'), ('b@x', 2, NULL)"))
        conn.execute(text("INSERT INTO orders (user_id, note) VALUES (1, 'first'), (1, 'second'), (2, 'third')"))
    engine.dispose()
    yield request.param, detect(url)


def users_of(db):
    schema = db.server_info().default_schema
    return Relation(schema, "users", "table"), Relation(schema, "orders", "table")


def emails(target):
    engine = create_engine(target.url)
    with engine.connect() as conn:
        found = [row[0] for row in conn.execute(text("SELECT email FROM users ORDER BY id"))]
    engine.dispose()
    return found


def test_detects_and_describes(server):
    name, target = server
    db = Database(target)
    assert db.server_info().product == {"postgresql": "PostgreSQL", "mysql": "MySQL"}[name]
    users, orders = users_of(db)
    details = db.describe(users)
    columns = {c.name: c for c in details.columns}
    assert columns["id"].primary_key and not columns["email"].nullable
    assert any(k.kind in ("unique", "unique index") and k.definition == "email" for k in details.keys)
    assert db.describe(orders).columns[1].references == "users.id"
    assert [fk.table for fk in db.referenced_by(users)] == ["orders"]


def test_queries_are_read_only(server):
    name, target = server
    db = Database(target)
    # A % in LIKE must reach the server untouched (psycopg and pymysql read % as a placeholder).
    assert db.run_query("SELECT email FROM users WHERE email LIKE 'a%'").rows == [("a@x",)]
    with pytest.raises(ReadOnlyError):
        db.run_query("DELETE FROM users")
    if name == "postgresql":
        assert db.run_query("SHOW transaction_read_only").rows == [("on",)]


def test_import_skips_a_failed_row_and_keeps_going(server):
    """On PostgreSQL a failed statement spoils the transaction; the savepoint per row keeps the rest."""
    _, target = server
    db = Database(replace(target, allow_writes=True))
    users, _ = users_of(db)
    planned = plan(parse_pasted("email\tscore\nc@x\t3\na@x\t9\nd@x\t1,234.50\n"), db.import_columns(users), "insert", ["id"])
    report = db.write_rows(users, "insert", ["id"], planned.ready)
    assert report.inserted == 2 and [f.line for f in report.failures] == [3]
    assert emails(target) == ["a@x", "b@x", "c@x", "d@x"]


def test_update_upsert_delete(server):
    _, target = server
    db = Database(replace(target, allow_writes=True))
    users, orders = users_of(db)
    columns = db.import_columns(users)

    def run(operation, pasted, key=("id",)):
        planned = plan(parse_pasted(pasted), columns, operation, list(key))
        return db.write_rows(users, operation, list(key), planned.ready)

    report = run("update", "email\tscore\nb@x\t20\nnope@x\t1\n", key=("email",))
    assert report.updated == 1 and report.failures[0].error == "No row with email = 'nope@x'."
    report = run("upsert", "id\temail\tscore\n2\tb@x\t21\n50\tz@x\t7\n")
    assert (report.updated, report.inserted) == (1, 1)
    order_columns = db.import_columns(orders)
    planned = plan(parse_pasted("id\n3\n"), order_columns, "delete", ["id"])
    assert db.write_rows(orders, "delete", ["id"], planned.ready).deleted == 1
    planned = plan(parse_pasted("id\n50\n77\n"), columns, "delete", ["id"])
    report = db.write_rows(users, "delete", ["id"], planned.ready)
    assert report.deleted == 1 and report.failures[0].line == 3
    assert emails(target) == ["a@x", "b@x"]


def test_export_streams_every_row(server):
    _, target = server
    db = Database(target)
    with db.stream_query("SELECT note FROM orders ORDER BY id", batch_size=2) as (columns, batches):
        assert columns == ["note"] and [len(batch) for batch in batches] == [2, 1]


def test_records(server):
    _, target = server
    db = Database(target)
    users, orders = users_of(db)
    assert db.fetch_row(users, {"id": 1})[1][1] == "a@x"
    assert db.count_rows(orders, {"user_id": 1}) == 2
    assert "WHERE user_id = 1" in db.select_sql(orders, {"user_id": 1})
