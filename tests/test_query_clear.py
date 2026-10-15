import asyncio

from udb_inspector.detect import detect
from udb_inspector.tui.app import InspectorApp


def test_ctrl_l_clears_the_editor_and_ctrl_z_undoes_it(sample_db):
    app = InspectorApp(detect(str(sample_db)))

    async def go():
        async with app.run_test(size=(140, 40)) as pilot:
            for _ in range(4):
                await app.workers.wait_for_complete()
                await pilot.pause()
            await pilot.press("f3")
            editor = app.query_panel.editor
            editor.text = "SELECT id, email FROM users"
            await pilot.press("ctrl+l")
            assert editor.text == "" and editor.has_focus
            await pilot.press("ctrl+z")
            assert editor.text == "SELECT id, email FROM users"

    asyncio.run(asyncio.wait_for(go(), 15))
