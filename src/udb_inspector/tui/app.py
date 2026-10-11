from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Group
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, Static, TabbedContent, TabPane, Tree
from textual.widgets.tree import TreeNode
from textual.worker import get_current_worker

from ..connection_setup import Saved
from ..database import ConnectError, Database, ForeignKey, Relation, ServerInfo, TableDetails, is_auth_error
from ..detect import DetectionError, Target
from ..render import connection_table, table_details
from ..settings import load_settings
from .query import QueryPanel
from .screens import ConnectionForm, ConnectionsScreen, PasswordScreen, ThemeScreen, key_hints


@dataclass(frozen=True)
class SchemaNode:
    name: str


NodeData = SchemaNode | Relation | None


class FilterInput(Input):
    BINDINGS = [Binding("down", "app.focus_tree", show=False)]


class InspectorApp(App):
    TITLE = "udbi"
    CSS_PATH = "app.tcss"
    AUTO_FOCUS = "#tree"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("c", "connections", "Connections"),
        Binding("a", "add_connection", "Add connection"),
        Binding("slash", "focus_filter", "Filter"),
        Binding("e", "query", "Query"),
        Binding("s", "select_rows", "Select rows"),
        Binding("i", "show_connection", "Info"),
        Binding("r", "reload", "Reload schema"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
    ]
    NEEDS_DATABASE = {"focus_filter", "focus_tree", "show_connection", "reload", "clear_filter", "query", "select_rows"}

    def __init__(self, target: Target | None = None, error: str | None = None, cwd: Path | None = None):
        super().__init__()
        self.cwd = cwd or Path.cwd()
        self.initial_target = target
        self.initial_error = error
        self.db: Database | None = None
        self.info: ServerInfo | None = None
        self.schemas: list[str] = []
        self.relations: dict[str, list[Relation]] = {}  # loaded schemas only
        self.expanded: set[str] = set()
        self.filter_text = ""
        self.details: TableDetails | None = None
        self.referenced_by: list[ForeignKey] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield FilterInput(placeholder="Filter tables  ( / )", id="filter")
                yield Tree[NodeData](Text("No database", style="dim"), id="tree")
            with TabbedContent(id="tabs"):
                with TabPane("Schema", id="schema-tab"):
                    with VerticalScroll(id="main"):
                        yield Static(id="details")
                with TabPane("Query", id="query-tab"):
                    yield QueryPanel(id="query")
        yield Footer()

    def on_mount(self) -> None:
        theme = load_settings().get("theme")
        if theme in self.available_themes:
            self.theme = theme
        if self.initial_target is not None:
            self.open_target(self.initial_target)
            return
        self._show_empty(self.initial_error)
        if self.initial_error:
            self.notify(self.initial_error, title="Couldn't open the connection", severity="error", timeout=10)
        self.action_connections()  # nothing to open: start on the chooser

    def on_unmount(self) -> None:
        if self.db is not None:
            self.db.close()

    def get_key_display(self, binding: Binding) -> str:
        # Textual writes ctrl+p as ^p, which reads as noise to most people.
        return super().get_key_display(binding).replace("^", "ctrl+")

    def action_change_theme(self) -> None:
        self.push_screen(ThemeScreen())

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if len(self.screen_stack) > 1:
            return action == "quit"  # a dialog is open; it handles its own keys
        if self.db is None and action in self.NEEDS_DATABASE:
            return False
        return True

    @property
    def tree(self) -> Tree[NodeData]:
        return self.query_one("#tree", Tree)

    @property
    def query_panel(self) -> QueryPanel:
        return self.query_one(QueryPanel)

    def _show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_query(self) -> None:
        self._show_tab("query-tab")
        self.query_panel.editor.focus()

    def action_select_rows(self) -> None:
        node = self.tree.cursor_node
        rel = node.data if node is not None and isinstance(node.data, Relation) else None
        rel = rel or (self.details.relation if self.details else None)
        if rel is None:
            self.notify("Highlight a table in the sidebar first.", timeout=4)
            return
        self.query_panel.set_sql(f"SELECT * FROM {self.db.quoted_name(rel)}")
        self._show_tab("query-tab")
        self.query_panel.action_run()

    def _root_label(self) -> Text:
        if self.db is None or self.info is None:
            return Text("No database", style="dim")
        database = self.db.target.url.database
        name = Path(database).name if self.info.dialect in ("sqlite", "duckdb") and database else database
        return Text(name or self.info.product, style="bold")

    # Connecting

    def action_connections(self) -> None:
        self.push_screen(ConnectionsScreen(self.cwd), self._chosen)

    def action_add_connection(self) -> None:
        self.push_screen(ConnectionForm(self.cwd), self._chosen)

    def _chosen(self, saved: Saved | None) -> None:
        if saved is None:
            return
        try:
            target = saved.target()
        except DetectionError as exc:
            self.notify(str(exc), title=f"Couldn't open {saved.name}", severity="error", timeout=10)
            return
        self.open_target(target)

    def open_target(self, target: Target, retrying: Target | None = None) -> None:
        """Connect in the background. `retrying` is the password-less target after a password prompt."""
        self.sub_title = f"connecting to {target.source}…"
        self.query_one("#main").loading = True
        self._connect(target, retrying)

    @work(thread=True, exclusive=True, group="connect")
    def _connect(self, target: Target, retrying: Target | None) -> None:
        try:
            db = Database(target)
        except ConnectError as exc:
            self.call_from_thread(self._connect_failed, target, exc, retrying)
            return
        try:
            info = db.server_info()
        except ConnectError as exc:
            db.close()
            self.call_from_thread(self._connect_failed, target, exc, retrying)
            return
        self.call_from_thread(self._connected, db, info)

    def _connect_failed(self, target: Target, exc: ConnectError, retrying: Target | None) -> None:
        self.query_one("#main").loading = False
        if is_auth_error(exc) and (retrying is not None or target.url.password is None):
            base = retrying or target
            error = "Wrong password, try again." if retrying else None

            def entered(password: str | None) -> None:
                if password is None:
                    self._connect_abandoned(None)
                else:
                    self.open_target(base.with_password(password), retrying=base)

            self.push_screen(PasswordScreen(base, error), entered)
            return
        self._connect_abandoned(str(exc))

    def _connect_abandoned(self, error: str | None) -> None:
        if self.db is None:
            self._show_empty(error)
        else:  # keep the database we already had open
            self._set_sub_title()
        if error:
            self.notify(error, title="Couldn't connect", severity="error", timeout=10)

    def _connected(self, db: Database, info: ServerInfo) -> None:
        previous = self.db
        self.db, self.info = db, info
        if previous is not None:
            previous.close()
        self._set_sub_title()
        self.tree.root.set_label(self._root_label())
        self.query_panel.reset()
        self.refresh_bindings()
        self.action_reload()

    def _set_sub_title(self) -> None:
        if self.db is None or self.info is None:
            self.sub_title = "no database"
        else:
            self.sub_title = f"{self.info.product} {self.info.version} · {self.db.target.source}"

    def _show_empty(self, error: str | None) -> None:
        self.workers.cancel_group(self, "relations")
        self.schemas, self.relations, self.expanded = [], {}, set()
        self.details = self.referenced_by = None
        self.tree.clear()
        self.tree.root.set_label(self._root_label())
        self.query_one("#main").loading = False
        parts = [Text("No database connected", style="bold"), Text("")]
        if error:
            parts += [Text(error, style="red"), Text("")]
        parts.append(key_hints(("c", "choose a saved connection"), ("a", "add a connection")))
        self.query_one("#details", Static).update(Group(*parts))
        self._set_sub_title()
        self.refresh_bindings()

    # Schema tree

    def action_reload(self) -> None:
        if self.db is None:
            return
        self.workers.cancel_group(self, "relations")
        self.db.clear_cache()
        self.schemas, self.relations, self.expanded = [], {}, set()
        self.tree.clear()
        self.tree.loading = True
        self.action_show_connection()
        self.load_schemas()

    @work(thread=True, exclusive=True, group="schemas")
    def load_schemas(self) -> None:
        try:
            schemas = self.db.schemas()
            first = self.db.relations(schemas[0]) if schemas else []
        except Exception as exc:
            self.call_from_thread(self._load_failed, exc)
            return
        if not get_current_worker().is_cancelled:
            self.call_from_thread(self._schemas_loaded, schemas, first)

    def _schemas_loaded(self, schemas: list[str], first: list[Relation]) -> None:
        self.tree.loading = False
        self.schemas = schemas
        if schemas:
            self.relations[schemas[0]] = first
            self.expanded.add(schemas[0])
        self._render_tree()
        if self.details is None:
            self.action_show_connection()

    def _render_tree(self) -> None:
        tree = self.tree
        tree.clear()
        tree.root.expand()
        if len(self.schemas) == 1:
            # A schema level with one entry (SQLite's "main") is just noise.
            self._add_relations(tree.root, self.relations.get(self.schemas[0], []))
            return
        for name in self.schemas:
            node = tree.root.add(Text(name, style="bold"), data=SchemaNode(name))
            if name in self.relations:
                matches = self._add_relations(node, self.relations[name])
                if (matches if self.filter_text else name in self.expanded):
                    node.expand()

    def _add_relations(self, node: TreeNode[NodeData], relations: list[Relation]) -> int:
        needle = self.filter_text.lower()
        matches = [rel for rel in relations if needle in rel.name.lower()]
        if not matches:
            node.add_leaf(Text("(no matches)" if relations else "(empty)", style="dim italic"))
        for rel in matches:
            label = Text(rel.name)
            if needle:
                start = rel.name.lower().index(needle)
                label.stylize("bold reverse", start, start + len(needle))
            if rel.kind == "view":
                label.append(" view", style="dim italic")
            node.add_leaf(label, data=rel)
        return len(matches)

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[NodeData]) -> None:
        data = event.node.data
        if not isinstance(data, SchemaNode):
            return
        self.expanded.add(data.name)
        if data.name not in self.relations and not event.node.children:
            event.node.add_leaf(Text("loading…", style="dim italic"))
            self.load_relations(event.node, data.name)

    def on_tree_node_collapsed(self, event: Tree.NodeCollapsed[NodeData]) -> None:
        if isinstance(event.node.data, SchemaNode):
            self.expanded.discard(event.node.data.name)

    @work(thread=True, group="relations")
    def load_relations(self, node: TreeNode[NodeData], schema: str) -> None:
        try:
            relations = self.db.relations(schema)
        except Exception as exc:
            self.call_from_thread(self._load_failed, exc)
            return
        if not get_current_worker().is_cancelled:
            self.call_from_thread(self._relations_loaded, node, schema, relations)

    def _relations_loaded(self, node: TreeNode[NodeData], schema: str, relations: list[Relation]) -> None:
        self.relations[schema] = relations
        node.remove_children()
        self._add_relations(node, relations)

    def _load_failed(self, exc: Exception) -> None:
        self.tree.loading = False
        self.query_one("#main").loading = False
        self.notify(str(exc), title="Couldn't read the schema", severity="error", timeout=10)

    # Filter

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_focus_tree(self) -> None:
        self.tree.focus()

    def action_clear_filter(self) -> None:
        self.query_one("#filter", Input).value = ""
        self.tree.focus()

    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value.strip()
        self._render_tree()

    @on(Input.Submitted, "#filter")
    def filter_submitted(self, event: Input.Submitted) -> None:
        self.tree.focus()
        first = next(self._relation_nodes(self.tree.root), None)
        if first is not None:
            self.tree.move_cursor(first)

    def _relation_nodes(self, node: TreeNode[NodeData]):
        for child in node.children:
            if isinstance(child.data, Relation):
                yield child
            yield from self._relation_nodes(child)

    # Main pane

    def on_tree_node_selected(self, event: Tree.NodeSelected[NodeData]) -> None:
        if self.db is None:
            return
        if isinstance(event.node.data, Relation):
            self._show_tab("schema-tab")
            self.query_one("#main").loading = True
            self.load_details(event.node.data)
        elif event.node.is_root:
            self.action_show_connection()

    @work(thread=True, exclusive=True, group="details")
    def load_details(self, rel: Relation) -> None:
        worker = get_current_worker()
        try:
            details = self.db.describe(rel)
        except Exception as exc:
            self.call_from_thread(self._load_failed, exc)
            return
        if worker.is_cancelled:
            return
        self.call_from_thread(self._show_details, details, None)
        if rel.kind != "table":
            return
        # Incoming foreign keys can take a while on big schemas, so they
        # arrive after the rest of the details.
        try:
            referenced_by = self.db.referenced_by(rel)
        except Exception as exc:
            self.call_from_thread(self._load_failed, exc)
            referenced_by = []
        if not worker.is_cancelled:
            self.call_from_thread(self._show_details, details, referenced_by)

    def _show_details(self, details: TableDetails, referenced_by: list[ForeignKey] | None) -> None:
        main = self.query_one("#main", VerticalScroll)
        main.loading = False
        if details != self.details:
            main.scroll_home(animate=False)
        self.details, self.referenced_by = details, referenced_by
        self.query_one("#details", Static).update(table_details(details, referenced_by))

    def action_show_connection(self) -> None:
        self._show_tab("schema-tab")
        self.workers.cancel_group(self, "details")
        self.details = self.referenced_by = None
        self.query_one("#main").loading = False
        self.query_one("#details", Static).update(
            Group(connection_table(self.db.target, self.info), Text(""), self._summary())
        )

    def _summary(self) -> Text:
        if not self.schemas:
            return Text("Reading schema…" if self.tree.loading else "No schemas visible to this user.", style="dim")
        first = self.relations.get(self.schemas[0], [])
        tables = sum(1 for rel in first if rel.kind == "table")
        text = f"{self.schemas[0]}: {_plural(tables, 'table')}, {_plural(len(first) - tables, 'view')}"
        if len(self.schemas) > 1:
            text += f" · {_plural(len(self.schemas), 'schema')} in total"
        return Text(text + ". Select a table to inspect it.", style="dim")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"
