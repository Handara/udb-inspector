import asyncio
import io

from rich.console import Console

from udb_inspector.database import Database
from udb_inspector.detect import detect
from udb_inspector.tui.app import InspectorApp


def make_app(sample_db):
    db = Database(detect(str(sample_db)))
    return InspectorApp(db, db.server_info())


def tree_labels(app):
    return [child.label.plain for child in app.tree.root.children]


def rendered(app):
    console = Console(file=io.StringIO(), width=120)
    console.print(app.query_one("#details").content)
    return console.file.getvalue()


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_sidebar_lists_tables_and_views(sample_db):
    app = make_app(sample_db)

    async def run():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            assert tree_labels(app) == ["orders", "users", "recent_orders view"]
            assert app.sub_title.startswith("SQLite ")
            assert "2 tables, 1 view" in rendered(app)

    asyncio.run(run())


def test_filter_narrows_tree(sample_db):
    app = make_app(sample_db)

    async def run():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("slash", "o", "r", "d")
            assert tree_labels(app) == ["orders", "recent_orders view"]
            await pilot.press("q")  # typed into the filter, doesn't quit
            assert app.is_running
            assert tree_labels(app) == ["(no matches)"]
            await pilot.press("escape")
            assert tree_labels(app) == ["orders", "users", "recent_orders view"]
            assert app.focused is app.tree

    asyncio.run(run())


def test_selecting_a_table_shows_its_details(sample_db):
    app = make_app(sample_db)

    async def run():
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("slash", *"users", "enter", "enter")
            await settle(app, pilot)
            assert app.details.relation.name == "users"
            assert [fk.table for fk in app.referenced_by] == ["orders"]
            text = rendered(app)
            assert "Referenced by" in text and "orders" in text and "age > 0" in text

            await pilot.press("i")
            assert app.details is None
            assert "SQLite" in rendered(app)

    asyncio.run(run())
