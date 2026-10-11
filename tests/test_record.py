import asyncio
import sqlite3

import pytest
from textual.widgets import DataTable

from udb_inspector.database import Database, Relation
from udb_inspector.detect import detect
from udb_inspector.tui.app import InspectorApp
from udb_inspector.tui.record import RecordScreen
from udb_inspector.tui.values import CellScreen


@pytest.fixture
def shop_db(tmp_path):
    path = tmp_path / "shop.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users (id), status TEXT);
        CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders (id), product TEXT);
        INSERT INTO users VALUES (1, 'ada@example.com'), (2, 'o''brien@example.com');
        INSERT INTO orders VALUES (1, 2, 'paid'), (2, 2, 'new'), (3, 1, 'new'), (4, NULL, 'lost');
        INSERT INTO order_items VALUES (1, 1, 'keyboard'), (2, 1, 'mouse'), (3, 2, 'cable');
        """
    )
    conn.close()
    return path


USERS = Relation("main", "users", "table")
ORDERS = Relation("main", "orders", "table")


def test_fetch_count_and_select_sql(shop_db):
    db = Database(detect(str(shop_db)))
    assert db.fetch_row(USERS, {"id": 2}) == (["id", "email"], (2, "o'brien@example.com"))
    assert db.fetch_row(USERS, {"id": 99}) is None
    assert db.count_rows(ORDERS, {"user_id": 2}) == 2
    assert db.select_sql(ORDERS, {"user_id": 2}) == "SELECT * FROM orders WHERE user_id = 2"
    assert db.select_sql(USERS, {"email": "o'brien@example.com"}) == "SELECT * FROM users WHERE email = 'o''brien@example.com'"


def run(app, scenario, size=(140, 45)):
    async def go():
        async with app.run_test(size=size) as pilot:
            await scenario(pilot)

    asyncio.run(asyncio.wait_for(go(), 20))


async def settle(pilot):
    for _ in range(5):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def record_rows(screen):
    table = screen.query_one(DataTable)
    return {
        str(key.value): [str(table.get_cell(key, column)) for column in ("field", "type", "value", "link")]
        for key in table.rows
    }


async def query(app, pilot, sql):
    await settle(pilot)
    await pilot.press("e")
    app.query_panel.editor.text = sql
    await pilot.press("ctrl+r")
    await settle(pilot)
    app.query_panel.grid.focus()


def test_open_a_record_and_follow_its_links(shop_db):
    app = InspectorApp(detect(str(shop_db)))

    async def scenario(pilot):
        await query(app, pilot, "SELECT id, status FROM orders ORDER BY id")
        await pilot.press("o")  # first row: order 1
        await settle(pilot)
        order = app.screen
        assert isinstance(order, RecordScreen) and order.rel.name == "orders"
        rows = record_rows(order)
        assert rows["field:id"][:3] == ["id", "INTEGER", "1"]  # fetched fresh, with every column
        assert rows["field:user_id"] == ["user_id", "INTEGER", "2", "→ users"]
        assert rows["in:0"][:3] == ["← order_items", "order_id", "2 rows"]
        assert "id = 1" in str(order.query_one("#record-title").content)

        await pilot.press("down", "enter")  # user_id → the user record
        await settle(pilot)
        user = app.screen
        assert isinstance(user, RecordScreen) and user.rel.name == "users"
        assert record_rows(user)["field:email"][2] == "o'brien@example.com"
        assert record_rows(user)["in:0"][:3] == ["← orders", "user_id", "2 rows"]

        await pilot.press("escape")
        assert app.screen is order
        await pilot.press("escape")
        assert not isinstance(app.screen, RecordScreen)

    run(app, scenario)


def test_related_rows_open_in_the_query_tab(shop_db):
    app = InspectorApp(detect(str(shop_db)))

    async def scenario(pilot):
        await query(app, pilot, "SELECT * FROM orders WHERE id = 1")
        await pilot.press("o")
        await settle(pilot)
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("in:0"))
        await pilot.press("enter")
        await settle(pilot)
        assert not isinstance(app.screen, RecordScreen)
        assert app.query_panel.editor.text == "SELECT * FROM order_items WHERE order_id = 1"
        assert [row[2] for row in app.query_panel.result.rows] == ["keyboard", "mouse"]
        assert app.focused is app.query_panel.grid

    run(app, scenario)


def test_values_copy_and_show_in_full(shop_db):
    app = InspectorApp(detect(str(shop_db)))

    async def scenario(pilot):
        await query(app, pilot, "SELECT * FROM users WHERE id = 1")
        await pilot.press("o")
        await settle(pilot)
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("field:email"))
        await pilot.press("c")
        assert app.clipboard == "ada@example.com"
        await pilot.press("enter")  # not a foreign key: the value in full
        assert isinstance(app.screen, CellScreen)

    run(app, scenario)


def test_a_null_foreign_key_has_no_link(shop_db):
    app = InspectorApp(detect(str(shop_db)))

    async def scenario(pilot):
        await query(app, pilot, "SELECT * FROM orders WHERE id = 4")
        await pilot.press("o")
        await settle(pilot)
        rows = record_rows(app.screen)
        assert rows["field:user_id"][2:] == ["NULL", ""]

    run(app, scenario)


def test_rows_without_the_primary_key_show_as_returned(shop_db):
    app = InspectorApp(detect(str(shop_db)))

    async def scenario(pilot):
        await query(app, pilot, "SELECT status FROM orders WHERE id = 1")
        await pilot.press("o")
        await settle(pilot)
        assert list(record_rows(app.screen)) == ["field:status"]
        assert "don't include the primary key" in str(app.screen.query_one("#record-note").content)

    run(app, scenario)


def test_joins_cant_open_a_record(shop_db):
    app = InspectorApp(detect(str(shop_db)))

    async def scenario(pilot):
        await query(app, pilot, "SELECT o.id, u.email FROM orders o JOIN users u ON u.id = o.user_id")
        await pilot.press("o")
        await pilot.pause()
        assert not isinstance(app.screen, RecordScreen)
        assert "reads several tables" in str(app.query_one("#status").content)

    run(app, scenario)
