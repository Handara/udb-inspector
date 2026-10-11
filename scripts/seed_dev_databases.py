"""Fill development databases with a shop schema and about a million rows each,
and save them as udbi connections (global, named dev-<name>, writes allowed).

    python scripts/seed_dev_databases.py sqlite=~/.local/share/udbi-dev/shop.sqlite3
    python scripts/seed_dev_databases.py postgres=postgresql://me@localhost:5433/udbi_dev mysql=mysql://...

Each NAME=URL creates (or replaces) the tables customers (100k rows), products
(1k), orders (1M) and order_items (1M) plus the view customer_totals, and saves
the connection dev-NAME. A bare path is a SQLite or DuckDB file, created if
needed. --rows changes the size of orders and order_items.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import os
import random
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
    insert,
)
from sqlalchemy.engine import URL

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from udb_inspector.connection_setup import load_or_new, save_connection, set_allow_writes  # noqa: E402
from udb_inspector.detect import parse_url  # noqa: E402

metadata = MetaData()
customers = Table(
    "customers", metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("name", String(100), nullable=False),
    Column("email", String(200), nullable=False, unique=True),
    Column("country", String(2), nullable=False),
    Column("signed_up", DateTime, nullable=False),
    Column("is_active", Boolean, nullable=False),
    Column("balance", Numeric(12, 2), nullable=False),
    Column("notes", Text),
)
products = Table(
    "products", metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("sku", String(20), nullable=False, unique=True),
    Column("name", String(100), nullable=False),
    Column("category", String(30), nullable=False),
    Column("price", Numeric(10, 2), nullable=False),
)
orders = Table(
    "orders", metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("customer_id", Integer, ForeignKey("customers.id"), nullable=False, index=True),
    Column("status", String(20), nullable=False),
    Column("total", Numeric(12, 2), nullable=False),
    Column("ordered_at", DateTime, nullable=False),
    Column("shipped_at", DateTime),
    Column("note", String(200)),
)
order_items = Table(
    "order_items", metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("order_id", Integer, ForeignKey("orders.id"), nullable=False, index=True),
    Column("product_id", Integer, ForeignKey("products.id"), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("unit_price", Numeric(10, 2), nullable=False),
)
VIEW = (
    "CREATE VIEW customer_totals AS SELECT customer_id, COUNT(*) AS order_count, SUM(total) AS total_spent "
    "FROM orders GROUP BY customer_id"
)

FIRST = ["Ada", "Grace", "Linus", "Guido", "Margaret", "Dennis", "Barbara", "Ken", "Alan", "Edsger", "Frances", "Tim",
         "Radia", "Donald", "Hedy", "John", "Katherine", "Bjarne", "Anita", "Niklaus", "Shafi", "Leslie", "Sophie", "Yukihiro"]
LAST = ["Lovelace", "Hopper", "Torvalds", "van Rossum", "Hamilton", "Ritchie", "Liskov", "Thompson", "Turing", "Dijkstra",
        "Allen", "Berners-Lee", "Perlman", "Knuth", "Lamarr", "McCarthy", "Johnson", "Stroustrup", "Borg", "Wirth",
        "Goldwasser", "Lamport", "Wilson", "Matsumoto"]
COUNTRIES = ["US", "FR", "DE", "MA", "RW", "JP", "BR", "IN", "GB", "CA", "NG", "ES"]
CATEGORIES = ["keyboards", "mice", "monitors", "cables", "audio", "storage", "laptops", "chairs"]
STATUSES = ["new", "paid", "shipped", "delivered", "cancelled", "refunded"]
START = datetime.datetime(2023, 1, 1)


def generate(rows: int):
    rng = random.Random(42)
    customer_count = max(1, rows // 10)

    def customer_rows():
        for i in range(1, customer_count + 1):
            first, last = rng.choice(FIRST), rng.choice(LAST)
            yield (i, f"{first} {last}", f"{first}.{last}.{i}@example.com".lower().replace(" ", ""), rng.choice(COUNTRIES),
                   START + datetime.timedelta(minutes=rng.randrange(1_500_000)), rng.random() > 0.1,
                   Decimal(rng.randrange(0, 1_000_000)) / 100, rng.choice([None] * 7 + ["VIP", "prefers email", "net 30"]))

    def product_rows():
        for i in range(1, 1001):
            category = CATEGORIES[i % len(CATEGORIES)]
            yield (i, f"SKU-{i:05}", f"{category[:-1].title()} model {i}", category, Decimal(rng.randrange(199, 250_000)) / 100)

    def order_rows():
        for i in range(1, rows + 1):
            status = rng.choice(STATUSES)
            ordered = START + datetime.timedelta(minutes=rng.randrange(1_500_000))
            shipped = ordered + datetime.timedelta(hours=rng.randrange(2, 200)) if status in ("shipped", "delivered") else None
            yield (i, rng.randint(1, customer_count), status, Decimal(rng.randrange(500, 500_000)) / 100, ordered, shipped,
                   rng.choice([None] * 9 + ["gift wrap", "leave at the door", "call first"]))

    def item_rows():
        for i in range(1, rows + 1):
            yield (i, rng.randint(1, rows), rng.randint(1, 1000), rng.randint(1, 5), Decimal(rng.randrange(199, 250_000)) / 100)

    return [(customers, customer_rows, customer_count), (products, product_rows, 1000), (orders, order_rows, rows),
            (order_items, item_rows, rows)]


def load(conn, table: Table, rows, dialect: str) -> None:
    """The fastest way each database takes a million rows."""
    names = [column.name for column in table.columns]
    if dialect == "postgresql":
        with conn.connection.cursor() as cursor, cursor.copy(f"COPY {table.name} ({', '.join(names)}) FROM STDIN") as copy:
            for row in rows():
                copy.write_row(row)
        return
    if dialect == "duckdb":
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
            writer = csv.writer(fh)
            writer.writerows(("" if value is None else value for value in row) for row in rows())
        conn.exec_driver_sql(f"COPY {table.name} FROM '{fh.name}' (HEADER false, NULLSTR '')")
        os.unlink(fh.name)
        return
    if dialect == "mssql" and hasattr(conn.connection.dbapi_connection, "bulk_copy"):  # pymssql
        conn.connection.dbapi_connection.bulk_copy(table.name, rows(), batch_size=10_000, tablock=True)
        return
    batch = []
    for row in rows():
        batch.append(dict(zip(names, row)))
        if len(batch) == 10_000:
            conn.execute(insert(table), batch)
            batch = []
    if batch:
        conn.execute(insert(table), batch)


def create_database(url: URL) -> None:
    """SQL Server containers start with only master, which has Microsoft's own tables in it."""
    engine = create_engine(url.set(database="master"), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.exec_driver_sql(f"IF DB_ID(N'{url.database}') IS NULL CREATE DATABASE [{url.database}]")
    engine.dispose()


def seed(name: str, url: URL, rows: int) -> None:
    if url.get_backend_name() == "mssql" and url.database not in (None, "master"):
        create_database(url)
    engine = create_engine(url)
    dialect = engine.dialect.name
    started = time.perf_counter()
    with engine.begin() as conn:
        if dialect == "oracle":  # no IF EXISTS before Oracle 23
            conn.exec_driver_sql("BEGIN EXECUTE IMMEDIATE 'DROP VIEW customer_totals'; EXCEPTION WHEN OTHERS THEN NULL; END;")
        else:
            conn.exec_driver_sql("DROP VIEW IF EXISTS customer_totals")
        metadata.drop_all(conn)
        metadata.create_all(conn)
    for table, rows_of, count in generate(rows):
        print(f"  {name}: {table.name} ({count:,} rows)…", flush=True)
        with engine.begin() as conn:
            load(conn, table, rows_of, dialect)
    with engine.begin() as conn:
        conn.exec_driver_sql(VIEW)
    engine.dispose()
    print(f"  {name}: done in {time.perf_counter() - started:.0f} s")


def save(name: str, url: URL) -> None:
    config = load_or_new("global", Path.cwd())
    connection = f"dev-{name}"
    if connection in config.connections:
        print(f"  {connection} is already a saved connection")
        return
    config = save_connection(config, connection, {"url": url.render_as_string(hide_password=False)}, {})
    set_allow_writes(config, connection, True)
    print(f"  saved as {connection} (writes allowed): udbi {connection}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("databases", nargs="+", metavar="NAME=URL")
    parser.add_argument("--rows", type=int, default=1_000_000, help="rows in orders and order_items (default: %(default)s)")
    args = parser.parse_args()
    for item in args.databases:
        name, _, raw = item.partition("=")
        if "://" not in raw:
            path = Path(raw).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = f"{'duckdb' if path.suffix == '.duckdb' else 'sqlite'}:///{path}"
        url = parse_url(raw)
        print(f"{name}: {url.render_as_string(hide_password=True)}")
        seed(name, url, args.rows)
        save(name, url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
