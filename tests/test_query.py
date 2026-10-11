import asyncio
import sqlite3

import pytest
from textual.widgets import DataTable, Input, Select

from udb_inspector.database import Database, QueryError
from udb_inspector.detect import detect
from udb_inspector.sql import ReadOnlyError, check_read_only, statement_at, statement_ranges, syntax_problem
from udb_inspector.tui.app import InspectorApp
from udb_inspector.tui.query import CellScreen


@pytest.fixture
def items_db(tmp_path):
    path = tmp_path / "items.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, price REAL, notes TEXT)")
    conn.executemany(
        "INSERT INTO items (name, price, notes) VALUES (?, ?, ?)",
        [(f"item {i:03}", None if i % 10 == 0 else i * 1.5, '{"tags": ["a", "b"]}' if i == 7 else None) for i in range(1, 121)],
    )
    conn.commit()
    conn.close()
    return path


# Statements


def test_statement_ranges_respect_strings_and_comments():
    text = "select 'a;b' from t;\n\nselect 2 -- c;\n;"
    assert statement_ranges(text, "postgres") == [(0, 19), (20, 37)]
    assert statement_at(text, 3, "postgres") == "select 'a;b' from t"
    assert statement_at(text, 25, "postgres") == "select 2 -- c;"
    assert statement_at(text, len(text), "postgres") == "select 2 -- c;"  # after the last one: the one before


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("postgresql", "SELECT 1"),
        ("postgresql", "WITH a AS (SELECT 1) SELECT * FROM a"),
        ("postgresql", "SELECT 1 UNION SELECT 2"),
        ("postgresql", "EXPLAIN SELECT 1"),
        ("postgresql", "EXPLAIN ANALYZE SELECT 1"),
        ("postgresql", "SHOW search_path"),
        ("postgresql", "VALUES (1)"),
        ("postgresql", "SELECT FROM WHERE ((("),  # unparseable: Postgres's read-only transaction decides
        ("mysql", "SHOW TABLES"),
        ("mysql", "DESCRIBE users"),
        ("sqlite", "PRAGMA table_info(users)"),
        ("mssql", "SELECT TOP 5 * FROM t"),
    ],
)
def test_reads_are_allowed(dialect, sql):
    check_read_only(sql, dialect)


@pytest.mark.parametrize(
    ("dialect", "sql", "reason"),
    [
        ("postgresql", "DELETE FROM x", "DELETE"),
        ("postgresql", "WITH d AS (DELETE FROM x RETURNING *) SELECT * FROM d", "contains a DELETE"),
        ("postgresql", "SELECT * INTO copy FROM x", "INTO creates a table"),
        ("postgresql", "EXPLAIN ANALYZE DELETE FROM x", "EXPLAIN ANALYZE runs"),
        ("postgresql", "SELECT 1; DELETE FROM x", "DELETE"),
        ("postgresql", "SET search_path TO a", "SET"),
        ("postgresql", "DO $$ BEGIN END $$", "DO"),
        ("mysql", "LOCK TABLES t READ", "LOCK TABLES"),
        ("mysql", "SELEC 1 FRM", "couldn't check"),  # MySQL commits DDL itself, so unparseable is refused
        ("sqlite", "PRAGMA journal_mode=WAL", "PRAGMA assignments"),
        ("mssql", "EXEC sp_who", "EXECUTE"),
        ("oracle", "GRANT SELECT ON t TO u", "GRANT"),
    ],
)
def test_writes_are_refused(dialect, sql, reason):
    with pytest.raises(ReadOnlyError, match=reason):
        check_read_only(sql, dialect)


def test_syntax_problem():
    assert syntax_problem("SELECT id FROM users", "postgresql") is None
    assert syntax_problem("SELECT id FROM users WHERE", "postgresql") == (
        "Required keyword: 'this' missing for Where near 'WHERE' (line 1, column 26)"
    )


# Running


def test_run_query(items_db):
    db = Database(detect(str(items_db)))
    result = db.run_query("SELECT id, name FROM items WHERE name LIKE 'item 01%' ORDER BY id", max_rows=5)
    assert result.columns == ["id", "name"]
    assert result.rows == [(10, "item 010"), (11, "item 011"), (12, "item 012"), (13, "item 013"), (14, "item 014")]
    assert result.truncated
    assert not db.run_query("SELECT 1").truncated


def test_queries_run_read_only(items_db):
    db = Database(detect(str(items_db)))
    assert db.run_query("PRAGMA query_only").rows == [(1,)]
    with pytest.raises(ReadOnlyError):
        db.run_query("DELETE FROM items")
    with pytest.raises(QueryError, match="syntax error"):
        db.run_query("SELEC 1")
    assert db.run_query("SELECT count(*) FROM items").rows == [(120,)]


# The Query tab


def run(app, scenario, size=(140, 45)):
    async def go():
        async with app.run_test(size=size) as pilot:
            await scenario(pilot)

    asyncio.run(asyncio.wait_for(go(), 20))


async def settle(pilot):
    for _ in range(4):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def grid_rows(app):
    grid = app.query_one(DataTable)
    return [[str(grid.get_cell_at((row, col))) for col in range(len(grid.columns))] for row in range(grid.row_count)]


def text_of(app, selector):
    return str(app.query_one(selector).content)


def test_run_the_statement_under_the_cursor(items_db):
    app = InspectorApp(detect(str(items_db)))

    async def scenario(pilot):
        await settle(pilot)
        await pilot.press("e")
        panel = app.query_panel
        assert app.focused is panel.editor
        panel.editor.text = "SELECT name FROM items WHERE id = 1;\nSELECT id FROM items WHERE id <= 3"
        panel.editor.move_cursor((0, 5))
        await pilot.press("ctrl+enter")
        await settle(pilot)
        assert grid_rows(app) == [["item 001"]]

        panel.editor.move_cursor((1, 3))
        await pilot.press("f5")
        await settle(pilot)
        assert grid_rows(app) == [["1"], ["2"], ["3"]]
        assert text_of(app, "#status").startswith("3 rows")

    run(app, scenario)


def test_s_selects_the_highlighted_table_with_pages(items_db):
    app = InspectorApp(detect(str(items_db)))

    async def scenario(pilot):
        await settle(pilot)
        await pilot.press("down", "s")  # the tree's first table is items
        await settle(pilot)
        panel = app.query_panel
        assert panel.editor.text == "SELECT * FROM items"
        assert len(grid_rows(app)) == 50
        assert "Rows 1–50 of 120  |  page 1 of 3" in text_of(app, "#pager")

        panel.grid.focus()
        await pilot.press("right_square_bracket", "right_square_bracket", "right_square_bracket")
        assert "Rows 101–120 of 120  |  page 3 of 3" in text_of(app, "#pager")
        await pilot.press("left_square_bracket")
        assert "page 2 of 3" in text_of(app, "#pager")

        panel.query_one("#page-size", Select).value = "all"
        await pilot.pause()
        assert len(grid_rows(app)) == 120

    run(app, scenario)


def test_search_and_sort(items_db):
    app = InspectorApp(detect(str(items_db)))

    async def scenario(pilot):
        await settle(pilot)
        await pilot.press("down", "s")
        await settle(pilot)
        panel = app.query_panel
        panel.grid.focus()
        await pilot.press("slash", *"item 11")
        await pilot.pause()
        assert [row[1] for row in grid_rows(app)] == ["item 110", "item 111", "item 112", "item 113", "item 114",
                                                     "item 115", "item 116", "item 117", "item 118", "item 119"]
        assert "10 match 'item 11'" in text_of(app, "#status")

        panel.query_one("#search", Input).value = ""
        await pilot.pause()
        grid = panel.grid
        columns = list(grid.columns.values())
        price_x = sum(column.get_render_width(grid) for column in columns[:2]) + 1
        await pilot.click("#grid", offset=(price_x, 0))  # the price header
        await pilot.pause()
        await pilot.click("#grid", offset=(price_x, 0))  # again: descending
        await pilot.pause()
        assert "price ▼" in str(columns[2].label) or "price ▼" in str(list(grid.columns.values())[2].label)
        prices = [row[2] for row in grid_rows(app)]
        assert prices[0] == "178.5"  # item 120 has no price, so 119 * 1.5 is the largest
        panel.query_one("#page-size", Select).value = "all"
        await pilot.pause()
        prices = [row[2] for row in grid_rows(app)]
        assert prices[-12:] == ["NULL"] * 12  # NULLs stay last when descending

    run(app, scenario)


def test_writes_are_refused_in_the_tab(items_db):
    app = InspectorApp(detect(str(items_db)))

    async def scenario(pilot):
        await settle(pilot)
        await pilot.press("e")
        app.query_panel.editor.text = "DELETE FROM items"
        await pilot.press("ctrl+r")
        await settle(pilot)
        assert "udbi is read-only" in text_of(app, "#status")

    run(app, scenario)
    assert Database(detect(str(items_db))).run_query("SELECT count(*) FROM items").rows == [(120,)]


def test_enter_shows_the_full_value(items_db):
    app = InspectorApp(detect(str(items_db)))

    async def scenario(pilot):
        await settle(pilot)
        await pilot.press("e")
        app.query_panel.editor.text = "SELECT notes FROM items WHERE id = 7"
        await pilot.press("ctrl+r")
        await settle(pilot)
        app.query_panel.grid.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, CellScreen)
        assert '"tags": [\n    "a",' in str(app.screen.query_one("#cell-value Static").content)
        await pilot.press("escape")
        assert not isinstance(app.screen, CellScreen)

    run(app, scenario)
