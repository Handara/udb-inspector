import asyncio
import io

import pytest
from rich.console import Console

from udb_inspector import cheatsheets, hints
from udb_inspector.detect import detect
from udb_inspector.language import SoqlLanguage, SqlLanguage
from udb_inspector.tui.app import InspectorApp, SchemaTree
from udb_inspector.tui.cheatsheet import CheatSheetScreen
from udb_inspector.tui.importer import ImportPanel
from udb_inspector.tui.query import QueryEditor, QueryPanel, SuggestionList
from udb_inspector.tui.record import RecordScreen
from udb_inspector.tui.screens import ConnectionsScreen
from udb_inspector.tui.values import CellScreen

DIALECTS = ["postgresql", "mysql", "mariadb", "sqlite", "duckdb", "mssql", "oracle"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_every_database_gets_every_page(dialect):
    pages = SqlLanguage(dialect).cheat_sheet_pages()
    assert [title for title, _ in pages] == ["Basics", *[title for title, _ in cheatsheets.SQL_PAGES], "udbi keys"]
    assert pages[0][1] == hints.cheat_sheet(dialect)
    assert all(topic and snippet for _, rows in pages for topic, snippet in rows)


def test_snippets_are_written_for_the_database():
    def entry(dialect, page, topic):
        return dict(dict(cheatsheets.sql_pages(dialect))[page])[topic]

    assert entry("oracle", "Joins", "In the first, not the second").startswith("SELECT id FROM a MINUS")
    assert entry("mariadb", "JSON", "Read a key") == "JSON_VALUE(doc, '$.name')"  # MariaDB has no ->>
    assert entry("mariadb", "Dates", "Add a month") == "d + INTERVAL 1 MONTH"  # MySQL's, when MariaDB has none of its own
    assert entry("sqlite", "Inspect", "Columns of a table") == "PRAGMA table_info(orders)"


def test_soql_pages():
    titles = [title for title, _ in SoqlLanguage().cheat_sheet_pages()]
    assert titles == ["Basics", "Relationships", "Filters", "Dates", "Aggregates", "Handy queries", "Gotchas", "sf CLI", "udbi keys"]


def test_the_keys_page_names_real_keys():
    bound = set()
    for owner in (InspectorApp, SchemaTree, QueryPanel, QueryEditor, SuggestionList, ImportPanel, RecordScreen,
                  ConnectionsScreen, CheatSheetScreen, CellScreen):
        for binding in owner.BINDINGS:
            bound.update(key.strip() for key in binding.key.split(","))
    named = {key for _, _, keys in cheatsheets.KEYS for key in keys}
    assert named - bound == set()


def run(app, scenario, size=(150, 45)):
    async def go():
        async with app.run_test(size=size) as pilot:
            await scenario(pilot)

    asyncio.run(asyncio.wait_for(go(), 20))


def shown(screen, selector):
    console = Console(file=io.StringIO(), width=150)
    console.print(screen.query_one(selector).content)
    return console.file.getvalue()


def test_turning_the_pages(sample_db, monkeypatch):
    monkeypatch.setattr(CheatSheetScreen, "last_page", {})
    app = InspectorApp(detect(str(sample_db)))

    async def scenario(pilot):
        for _ in range(4):
            await app.workers.wait_for_complete()
            await pilot.pause()
        await pilot.press("f3", "f1")
        screen = app.screen
        assert isinstance(screen, CheatSheetScreen) and "SQLite cheat sheet" in str(screen.query_one(".title").content)
        assert "1 Basics" in shown(screen, "#sheet-tabs") and "First 10 rows" in shown(screen, "#sheet-page")
        await pilot.press("right")
        assert "LEFT JOIN payments" in shown(screen, "#sheet-page")
        await pilot.press("left", "left")  # around to the last page
        assert "ctrl+arrows" in shown(screen, "#sheet-page")
        await pilot.press("9")
        assert "PRAGMA table_info(orders)" in shown(screen, "#sheet-page")
        await pilot.press("escape")
        await pilot.press("f1")  # opens where it was left
        assert "PRAGMA table_info(orders)" in shown(app.screen, "#sheet-page")

    run(app, scenario)
