"""Create a small shop database in SQLite to try udbi against.

    python scripts/make_demo_db.py [demo.sqlite3]
"""

import random
import sqlite3
import sys
from datetime import date, timedelta

SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(150) NOT NULL UNIQUE,
    email VARCHAR(254) NOT NULL,
    is_staff BOOLEAN NOT NULL DEFAULT 0,
    date_joined DATETIME NOT NULL
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku VARCHAR(32) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    price DECIMAL(10, 2) NOT NULL
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id),
    status VARCHAR(20) NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE INDEX orders_user_id ON orders (user_id);
CREATE TABLE order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders (id),
    product_id INTEGER NOT NULL REFERENCES products (id),
    quantity INTEGER NOT NULL
);
CREATE VIEW order_totals AS
    SELECT o.id AS order_id, o.status, SUM(i.quantity * p.price) AS total
    FROM orders o
    JOIN order_items i ON i.order_id = o.id
    JOIN products p ON p.id = i.product_id
    GROUP BY o.id;
"""


def build(path: str) -> None:
    rng = random.Random(42)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    names = ["ada", "grace", "linus", "guido", "margaret", "dennis", "barbara", "ken"]
    for i, name in enumerate(names):
        joined = date(2025, 1, 1) + timedelta(days=i * 17)
        conn.execute(
            "INSERT INTO users (username, email, is_staff, date_joined) VALUES (?, ?, ?, ?)",
            (name, f"{name}@example.com", i == 0, joined.isoformat()),
        )
    products = [("KB-01", "Keyboard", 49.90), ("MS-02", "Mouse", 19.50), ("MN-27", "Monitor 27in", 229.00),
                ("CB-USB", "USB-C cable", 9.99), ("HD-01", "Headset", 89.00)]
    conn.executemany("INSERT INTO products (sku, name, price) VALUES (?, ?, ?)", products)
    for order_id in range(1, 41):
        created = date(2026, 1, 1) + timedelta(days=rng.randrange(270))
        conn.execute(
            "INSERT INTO orders (user_id, status, created_at) VALUES (?, ?, ?)",
            (rng.randint(1, len(names)), rng.choice(["new", "paid", "shipped", "cancelled"]), created.isoformat()),
        )
        for product_id in rng.sample(range(1, len(products) + 1), rng.randint(1, 3)):
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, quantity) VALUES (?, ?, ?)",
                (order_id, product_id, rng.randint(1, 4)),
            )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "demo.sqlite3"
    build(out)
    print(f"wrote {out}")
