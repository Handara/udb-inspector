from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Static, Tree
from textual.widgets.tree import TreeNode
from textual.worker import get_current_worker

from ..database import Database, Relation, ServerInfo
from ..render import connection_table


@dataclass
class SchemaNode:
    name: str
    loaded: bool = False


NodeData = SchemaNode | Relation | None


class InspectorApp(App):
    TITLE = "dbi"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "reload", "Reload schema"),
    ]

    def __init__(self, db: Database, info: ServerInfo):
        super().__init__()
        self.db = db
        self.info = info

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Tree[NodeData](self._root_label(), id="sidebar")
            with VerticalScroll(id="main"):
                yield Static(connection_table(self.db.target, self.info), id="connection")
                yield Static(id="summary")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"{self.info.product} {self.info.version}".strip()
        self.sidebar.focus()
        self.action_reload()

    @property
    def sidebar(self) -> Tree[NodeData]:
        return self.query_one("#sidebar", Tree)

    def _root_label(self) -> Text:
        database = self.db.target.url.database
        name = Path(database).name if self.info.dialect in ("sqlite", "duckdb") and database else database
        return Text(name or self.info.product, style="bold")

    def action_reload(self) -> None:
        self.workers.cancel_group(self, "relations")
        self.sidebar.clear()
        self.sidebar.root.expand()
        self.sidebar.loading = True
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
            self.call_from_thread(self._show_schemas, schemas, first)

    def _show_schemas(self, schemas: list[str], first: list[Relation]) -> None:
        root = self.sidebar.root
        self.sidebar.loading = False
        if len(schemas) == 1:
            # A schema level with one entry (SQLite's "main") is just noise.
            self._add_relations(root, first)
        else:
            for index, name in enumerate(schemas):
                node = root.add(Text(name, style="bold"), data=SchemaNode(name, loaded=index == 0))
                if index == 0:
                    self._add_relations(node, first)
                    node.expand()
        self.query_one("#summary", Static).update(self._summary(schemas, first))

    def _summary(self, schemas: list[str], first: list[Relation]) -> Text:
        if not schemas:
            return Text("No schemas visible to this user.")
        tables = sum(1 for rel in first if rel.kind == "table")
        views = len(first) - tables
        text = f"{schemas[0]}: {_plural(tables, 'table')}, {_plural(views, 'view')}"
        if len(schemas) > 1:
            text += f" · {_plural(len(schemas), 'schema')} in total"
        return Text(text, style="dim")

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[NodeData]) -> None:
        data = event.node.data
        if isinstance(data, SchemaNode) and not data.loaded:
            data.loaded = True
            event.node.add_leaf(Text("loading…", style="dim italic"))
            self.load_relations(event.node, data.name)

    @work(thread=True, group="relations")
    def load_relations(self, node: TreeNode[NodeData], schema: str) -> None:
        try:
            relations = self.db.relations(schema)
        except Exception as exc:
            self.call_from_thread(self._load_failed, exc)
            return
        if not get_current_worker().is_cancelled:
            self.call_from_thread(self._fill_schema, node, relations)

    def _fill_schema(self, node: TreeNode[NodeData], relations: list[Relation]) -> None:
        node.remove_children()
        self._add_relations(node, relations)

    def _add_relations(self, node: TreeNode[NodeData], relations: list[Relation]) -> None:
        if not relations:
            node.add_leaf(Text("(empty)", style="dim italic"))
        for rel in relations:
            label = Text(rel.name) if rel.kind == "table" else Text.assemble(rel.name, (" view", "dim italic"))
            node.add_leaf(label, data=rel)

    def _load_failed(self, exc: Exception) -> None:
        self.sidebar.loading = False
        self.notify(str(exc), title="Couldn't read the schema", severity="error", timeout=10)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"
