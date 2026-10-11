import asyncio
import io

import pytest
from rich.console import Console

from udb_inspector.detect import detect
from udb_inspector.hints import CHEAT_SHEET_TOPICS, CHEAT_SHEETS, TableRef, cheat_sheet, context_at, notes, suggest, tables_in
from udb_inspector.tui.app import InspectorApp
from udb_inspector.tui.query import CheatSheetScreen

TABLES = [("", "users", "table"), ("", "orders", "table"), ("", "order_totals", "view")]
COLUMNS = {
    "users": [("id", "INTEGER"), ("email", "TEXT"), ("age", "INTEGER")],
    "orders": [("id", "INTEGER"), ("user_id", "INTEGER"), ("status", "VARCHAR(20)")],
}


def labels(sql, dialect="postgresql"):
    """Suggestion labels with the cursor at |."""
    context = context_at(sql.replace("|", ""), sql.index("|"), dialect)
    return [s.label for s in suggest(context, dialect, TABLES, lambda ref: COLUMNS.get(ref.name))]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("|", ["SELECT", "WITH", "EXPLAIN", "VALUES", "SHOW"]),
        ("SEL|", ["SELECT"]),
        ("SELECT * FROM |", ["users", "orders", "order_totals"]),
        ("SELECT * FROM or|", ["orders", "order_totals"]),
        ("SELECT * FROM users, |", ["users", "orders", "order_totals"]),
        ("SELECT em| FROM users", ["email"]),
        ("SELECT u.| FROM users u JOIN orders o ON o.user_id = u.id", ["id", "email", "age"]),
        ("SELECT o.st| FROM users u JOIN orders AS o ON 1=1", ["status"]),
        ("SELECT * FROM users wh|", ["WHERE", "WHEN"]),
        ("SELECT 'abc| FROM users", []),  # inside a string
        ("SELECT 1 -- note |", []),  # inside a comment
        ("SELECT 1; SELECT * FROM |", ["users", "orders", "order_totals"]),
    ],
)
def test_suggestions(sql, expected):
    assert labels(sql) == expected


def test_columns_come_first_then_keywords_after_an_expression():
    assert labels("SELECT | FROM users")[:4] == ["id", "email", "age", "COUNT()"]
    assert labels("SELECT id | FROM users")[:2] == ["FROM", "WHERE"]


def test_limit_keywords_follow_the_database():
    assert "LIMIT" in labels("SELECT * FROM users |", "postgresql")
    assert "LIMIT" not in labels("SELECT * FROM users |", "mssql")
    assert "FETCH NEXT" in labels("SELECT * FROM users |", "mssql")
    assert "FETCH FIRST" in labels("SELECT * FROM users |", "oracle")


def test_an_exact_match_alone_is_not_suggested():
    assert labels("SELECT * FROM users|") == []


def test_tables_in():
    sql = 'SELECT * FROM public.orders AS o LEFT JOIN "Users" u ON u.id = o.user_id'
    assert tables_in(sql, "postgres") == [TableRef("public", "orders", "o"), TableRef(None, "Users", "u")]
    assert tables_in("SELECT * FROM a x, b", "postgres") == [TableRef(None, "a", "x"), TableRef(None, "b", None)]


@pytest.mark.parametrize(
    ("dialect", "sql", "expected"),
    [
        ("mssql", "SELECT * FROM t LIMIT 5", "SQL Server has no LIMIT"),
        ("oracle", "SELECT * FROM t LIMIT 5", "Oracle has no LIMIT"),
        ("postgresql", "SELECT TOP 5 * FROM t", "TOP is SQL Server syntax. Use LIMIT"),
        ("mysql", "SELECT a || b FROM t", "|| means OR"),
        ("mysql", "SELECT * FROM t WHERE x ILIKE 'a'", "LIKE already ignores case"),
        ("sqlite", "SELECT * FROM t WHERE x ILIKE 'a'", "SQLite has no ILIKE"),
        ("postgresql", 'SELECT * FROM t WHERE name = "bob"', '"bob" names a column'),
        ("mssql", "SELECT NOW() FROM t", "Use GETDATE()"),
        ("mssql", "SELECT LENGTH(x) FROM t", "SQL Server uses LEN()"),
        ("postgresql", "SELECT IFNULL(a, 1) FROM t", "COALESCE(a, b) works everywhere"),
        ("oracle", "SELECT `a` FROM t", "backticks"),
    ],
)
def test_notes(dialect, sql, expected):
    assert any(expected in note for note in notes(sql, dialect))


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("mysql", 'SELECT * FROM t WHERE name = "bob"'),  # a string in MySQL
        ("sqlite", "SELECT IFNULL(a, 1) FROM t"),
        ("mssql", "SELECT TOP 5 * FROM t"),
        ("postgresql", "SELECT * FROM t WHERE x ILIKE 'a%' LIMIT 5"),
    ],
)
def test_no_notes_for_native_syntax(dialect, sql):
    assert notes(sql, dialect) == []


def test_every_database_has_a_full_cheat_sheet():
    for dialect, snippets in CHEAT_SHEETS.items():
        assert len(snippets) == len(CHEAT_SHEET_TOPICS), dialect
    assert cheat_sheet("mssql")[0] == ("First 10 rows", "SELECT TOP 10 …")


# In the editor


def run(app, scenario, size=(140, 45)):
    async def go():
        async with app.run_test(size=size) as pilot:
            await scenario(pilot)

    asyncio.run(asyncio.wait_for(go(), 20))


async def settle(pilot):
    for _ in range(4):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def hint_text(app):
    console = Console(file=io.StringIO(), width=300)  # wide, so nothing is cut with "…"
    console.print(app.query_one("#hints").content)
    return console.file.getvalue()


def test_tab_opens_the_list_and_enter_inserts(sample_db):
    from udb_inspector.tui.query import SuggestionList

    app = InspectorApp(detect(str(sample_db)))

    async def scenario(pilot):
        await settle(pilot)
        await pilot.press("e")
        editor = app.query_panel.editor
        listing = app.query_panel.query_one(SuggestionList)
        editor.insert("SELECT * FROM us")
        await pilot.pause()
        assert "Tables:  users" in hint_text(app)
        await pilot.press("tab")
        assert app.focused is listing and listing.highlighted == 0
        await pilot.press("enter")
        assert editor.text == "SELECT * FROM users"
        assert app.focused is editor and not listing.display

        editor.insert(" u WHERE u.")
        await settle(pilot)  # the columns load in the background
        assert "Columns of u:  id INTEGER  |  email TEXT  |  age INTEGER" in hint_text(app)
        await pilot.press("tab", "tab", "tab", "shift+tab")  # open, down, down, up
        assert listing.highlighted == 1
        await pilot.press("enter")
        assert editor.text == "SELECT * FROM users u WHERE u.email"

        editor.insert(" = 'x'\n")
        await pilot.pause()
        await pilot.press("tab")  # nothing to suggest: tab indents
        assert editor.text.endswith("\n    ")

    run(app, scenario)


def test_the_list_gives_way_to_typing_and_esc(sample_db):
    from udb_inspector.tui.query import SuggestionList

    app = InspectorApp(detect(str(sample_db)))

    async def scenario(pilot):
        await settle(pilot)
        await pilot.press("e")
        editor = app.query_panel.editor
        listing = app.query_panel.query_one(SuggestionList)
        editor.insert("SELECT * FROM o")
        await pilot.pause()
        await pilot.press("tab", "r")  # keep typing: the r goes to the editor
        assert editor.text == "SELECT * FROM or" and app.focused is editor and not listing.display
        await pilot.press("tab", "escape")  # esc: back without inserting
        assert editor.text == "SELECT * FROM or" and app.focused is editor and not listing.display

    run(app, scenario)


def test_esc_hides_suggestions_then_leaves_the_editor(sample_db):
    app = InspectorApp(detect(str(sample_db)))

    async def scenario(pilot):
        await settle(pilot)
        await pilot.press("e")
        editor = app.query_panel.editor
        editor.insert("SELECT * FROM us")
        await pilot.pause()
        await pilot.press("escape")
        assert "Tables:" not in hint_text(app)
        assert app.focused is editor
        await pilot.press("escape")  # nothing left to hide: leave the editor
        assert app.focused is not editor

    run(app, scenario)


def test_notes_and_syntax_problems_show_under_the_editor(sample_db):
    app = InspectorApp(detect(str(sample_db)))

    async def scenario(pilot):
        await settle(pilot)
        await pilot.press("e")
        editor = app.query_panel.editor
        editor.insert("SELECT * FROM users WHERE email ILIKE 'a%'")
        await pilot.pause()
        assert "SQLite has no ILIKE" in hint_text(app)

        editor.text = "SELECT * FROM users WHERE"
        await pilot.pause(0.9)  # the syntax check waits for a pause in typing
        assert "⚠ Required keyword" in hint_text(app)

        await pilot.press("f1")
        assert isinstance(app.screen, CheatSheetScreen)
        assert "SQLite cheat sheet" in str(app.screen.query_one(".title").content)

    run(app, scenario)
