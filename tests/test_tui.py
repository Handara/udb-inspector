import asyncio
import io

from rich.console import Console
from sqlalchemy.engine import URL
from textual.widgets import Input, OptionList, Select

from udb_inspector.connections import global_dir
from udb_inspector.database import ConnectError, Database
from udb_inspector.detect import Target, detect
from udb_inspector.tui import app as app_module
from udb_inspector.tui.app import InspectorApp
from udb_inspector.tui.screens import ConnectionForm, ConnectionsScreen, PasswordScreen


def run(app, scenario, size=(120, 40)):
    async def go():
        async with app.run_test(size=size) as pilot:
            await scenario(pilot)

    asyncio.run(asyncio.wait_for(go(), 15))


async def settle(pilot):
    # Connecting starts a worker that starts the schema worker.
    for _ in range(4):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def tree_labels(app):
    return [child.label.plain for child in app.tree.root.children]


def rendered(app):
    console = Console(file=io.StringIO(), width=120)
    console.print(app.query_one("#details").content)
    return console.file.getvalue()


def write_global(text):
    path = global_dir() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_sidebar_lists_tables_and_views(sample_db):
    app = InspectorApp(detect(str(sample_db)))

    async def scenario(pilot):
        await settle(pilot)
        assert tree_labels(app) == ["orders", "users", "recent_orders view"]
        assert app.sub_title.startswith("SQLite ")
        assert "2 tables, 1 view" in rendered(app)

    run(app, scenario)


def test_filter_narrows_tree(sample_db):
    app = InspectorApp(detect(str(sample_db)))

    async def scenario(pilot):
        await settle(pilot)
        await pilot.press("slash", "o", "r", "d")
        assert tree_labels(app) == ["orders", "recent_orders view"]
        await pilot.press("q")  # typed into the filter, doesn't quit
        assert app.is_running
        assert tree_labels(app) == ["(no matches)"]
        await pilot.press("escape")
        assert tree_labels(app) == ["orders", "users", "recent_orders view"]
        assert app.focused is app.tree

    run(app, scenario)


def test_selecting_a_table_shows_its_details(sample_db):
    app = InspectorApp(detect(str(sample_db)))

    async def scenario(pilot):
        await settle(pilot)
        await pilot.press("slash", *"users", "enter", "enter")
        await settle(pilot)
        assert app.details.relation.name == "users"
        assert [fk.table for fk in app.referenced_by] == ["orders"]
        text = rendered(app)
        assert "Referenced by" in text and "orders" in text and "age > 0" in text

        await pilot.press("i")
        assert app.details is None
        assert "SQLite" in rendered(app)

    run(app, scenario)


def test_no_connection_starts_on_the_chooser(tmp_path):
    app = InspectorApp(None, cwd=tmp_path)

    async def scenario(pilot):
        await pilot.pause()
        assert isinstance(app.screen, ConnectionsScreen)
        prompts = [str(option.prompt) for option in app.screen.query_one(OptionList).options]
        assert prompts == ["No saved connections yet.", "+ Add a connection"]

        await pilot.press("escape")
        assert not isinstance(app.screen, ConnectionsScreen)
        assert "No database connected" in rendered(app)
        assert app.sub_title == "no database"
        assert app.check_action("reload", ()) is False
        assert app.check_action("connections", ()) is True

    run(app, scenario)


def test_chooser_opens_a_saved_connection(tmp_path, sample_db):
    write_global(f'[connections.staging]\nurl = "sqlite:///{sample_db}"\n')
    app = InspectorApp(None, cwd=tmp_path)

    async def scenario(pilot):
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)
        assert app.sub_title.endswith("global [staging]")
        assert tree_labels(app) == ["orders", "users", "recent_orders view"]

    run(app, scenario)


def test_add_a_global_connection_in_the_form(tmp_path, sample_db):
    app = InspectorApp(None, cwd=tmp_path)

    async def scenario(pilot):
        await pilot.pause()
        await pilot.press("a")  # from the chooser
        form = app.screen
        assert isinstance(form, ConnectionForm)
        assert form.scope == "global"  # no .udbi.toml or .env here
        form.query_one("#name", Input).value = "demo"
        form.query_one("#type", Select).value = "sqlite"
        await pilot.pause()
        assert not form.query_one("#row-host").display
        form.query_one("#database", Input).value = str(sample_db)
        await pilot.press("ctrl+s")
        await settle(pilot)

        assert app.sub_title.endswith("global [demo]")
        assert tree_labels(app) == ["orders", "users", "recent_orders view"]
        config = (global_dir() / "config.toml").read_text()
        assert '[connections.demo]\ntype = "sqlite"' in config

    run(app, scenario)


def test_form_fills_in_from_env(tmp_path, sample_db):
    (tmp_path / ".env").write_text(f"DATABASE_URL=sqlite:///{sample_db}\n")
    app = InspectorApp(None, cwd=tmp_path)

    async def scenario(pilot):
        await pilot.pause()
        await pilot.press("escape", "a")  # close the chooser, add from the main screen
        form = app.screen
        assert form.scope == "project"
        assert form.query_one("#name", Input).value == "default"
        form.query_one("#guess", Select).value = "0"  # "url from DATABASE_URL"
        await pilot.pause()
        assert form.query_one("#url", Input).value == "${DATABASE_URL}"
        assert form.query_one("#row-url").display
        await pilot.press("ctrl+s")
        await settle(pilot)

        assert '[connections.default]\nurl = "${DATABASE_URL}"' in (tmp_path / ".udbi.toml").read_text()
        assert tree_labels(app) == ["orders", "users", "recent_orders view"]

    run(app, scenario)


def test_form_refuses_a_password_in_the_project_file(tmp_path):
    (tmp_path / ".env").touch()
    app = InspectorApp(None, cwd=tmp_path)

    async def scenario(pilot):
        await pilot.pause()
        await pilot.press("a")
        form = app.screen
        form.query_one("#host", Input).value = "db"
        form.query_one("#password", Input).value = "hunter2"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, ConnectionForm)
        assert "${VAR}" in str(form.query_one("#status").content)
        assert not (tmp_path / ".udbi.toml").exists()

    run(app, scenario)


def test_typing_in_a_dialog_does_not_filter_the_sidebar(sample_db, tmp_path):
    app = InspectorApp(detect(str(sample_db)), cwd=tmp_path)

    async def scenario(pilot):
        await settle(pilot)
        await pilot.press("a", *"xyz")
        assert app.filter_text == ""
        await pilot.press("escape")
        assert tree_labels(app) == ["orders", "users", "recent_orders view"]

    run(app, scenario)


def test_password_dialog(sample_db, monkeypatch):
    class NeedsPassword(Database):
        def __init__(self, target):
            self.password = target.url.password
            super().__init__(detect(str(sample_db)))

        def server_info(self):
            if self.password != "pw":
                raise ConnectError("FATAL: password authentication failed")
            return super().server_info()

    monkeypatch.setattr(app_module, "Database", NeedsPassword)
    target = Target(URL.create("postgresql+psycopg", username="app", host="db", database="shop"), "test")
    app = InspectorApp(target)

    async def scenario(pilot):
        await settle(pilot)
        assert isinstance(app.screen, PasswordScreen)
        await pilot.press(*"nope", "enter")
        await settle(pilot)
        assert isinstance(app.screen, PasswordScreen)
        assert app.screen.error == "Wrong password, try again."
        await pilot.press("p", "w", "enter")
        await settle(pilot)
        assert not isinstance(app.screen, PasswordScreen)
        assert tree_labels(app) == ["orders", "users", "recent_orders view"]

    run(app, scenario)


def test_failed_connection_shows_the_error(tmp_path):
    app = InspectorApp(detect(f"sqlite:///{tmp_path / 'gone.db'}"))

    async def scenario(pilot):
        await settle(pilot)
        assert "No database connected" in rendered(app)
        assert "does not exist" in rendered(app)

    run(app, scenario)


def test_delete_from_the_chooser(tmp_path):
    path = write_global('[connections.one]\nurl = "sqlite://"\n\n[connections.two]\nurl = "sqlite://"\n')
    app = InspectorApp(None, cwd=tmp_path)

    async def scenario(pilot):
        await pilot.pause()
        await pilot.press("d", "y")
        await pilot.pause()
        assert "[connections.one]" not in path.read_text()
        assert "[connections.two]" in path.read_text()
        assert [s.name for s in app.screen.saved] == ["two"]

    run(app, scenario)


def test_q_quits_from_the_chooser(tmp_path):
    app = InspectorApp(None, cwd=tmp_path)

    async def scenario(pilot):
        await pilot.pause()
        assert isinstance(app.screen, ConnectionsScreen)
        await pilot.press("q")
        await pilot.pause()
        assert app.return_code == 0

    run(app, scenario)
