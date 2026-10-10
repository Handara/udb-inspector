from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Group
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, Static, Tree
from textual.widgets.tree import TreeNode
from textual.worker import get_current_worker

from ..database import Database, ForeignKey, Relation, ServerInfo, TableDetails
from ..render import connection_table, table_details


@dataclass(frozen=True)
class SchemaNode:
    name: str


NodeData = SchemaNode | Relation | None


class FilterInput(Input):
    BINDINGS = [Binding("down", "app.focus_tree", show=False)]


class InspectorApp(App):
    TITLE = "dbi"
    CSS_PATH = "app.tcss"
    AUTO_FOCUS = "#tree"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "focus_filter", "Filter"),
        Binding("i", "show_connection", "Connection"),
        Binding("r", "reload", "Reload schema"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
    ]

    def __init__(self, db: Database, info: ServerInfo):
        super().__init__()
        self.db = db
        self.info = info
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
                yield Tree[NodeData](self._root_label(), id="tree")
            with VerticalScroll(id="main"):
                yield Static(id="details")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"{self.info.product} {self.info.version}".strip()
        self.action_reload()

    @property
    def tree(self) -> Tree[NodeData]:
        return self.query_one("#tree", Tree)

    def _root_label(self) -> Text:
        database = self.db.target.url.database
        name = Path(database).name if self.info.dialect in ("sqlite", "duckdb") and database else database
        return Text(name or self.info.product, style="bold")

    # Schema tree

    def action_reload(self) -> None:
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

    def on_input_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value.strip()
        self._render_tree()

    def on_input_submitted(self, event: Input.Submitted) -> None:
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
        if isinstance(event.node.data, Relation):
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
