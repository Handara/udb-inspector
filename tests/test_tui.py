import asyncio

from db_inspector.database import Database
from db_inspector.detect import resolve
from db_inspector.tui.app import InspectorApp


def test_sidebar_lists_tables_and_views(sample_db):
    db = Database(resolve(str(sample_db)))
    app = InspectorApp(db, db.server_info())

    async def run():
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            root = app.sidebar.root
            return [child.label.plain for child in root.children], app.sub_title

    labels, sub_title = asyncio.run(run())
    assert labels == ["orders", "users", "recent_orders view"]
    assert sub_title.startswith("SQLite ")
