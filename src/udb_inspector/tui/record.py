"""One row of a table, with its foreign keys followed both ways."""

from __future__ import annotations

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label, Static

from ..database import ForeignKey, QueryError, Relation, TableDetails, qualified
from .screens import key_hints
from .values import CellScreen, cell, full_text


class RecordScreen(ModalScreen[None]):
    """Fields of one record. Enter on a foreign key opens the record it points to;
    enter on a related table queries the rows that point here."""

    BINDINGS = [
        Binding("escape,backspace", "back", "Back"),
        Binding("c", "copy", "Copy value"),
    ]

    def __init__(self, rel: Relation, where: dict[str, object] | None = None, row: dict[str, object] | None = None):
        """Fetch the record where its columns equal `where`, or show `row` as it is
        when it can't be fetched (no primary key among the query's columns)."""
        super().__init__()
        self.rel = rel
        self.where = where
        self.row = row
        self.values: dict[str, object] = {}
        self.outgoing: dict[str, ForeignKey] = {}  # column -> foreign key it belongs to
        self.incoming: list[ForeignKey] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog record"):
            yield Label(self.rel.name, classes="title", id="record-title", markup=False)
            yield Static(id="record-note", classes="hint")
            yield DataTable(id="record", cursor_type="row", zebra_stripes=True)
            yield Label(key_hints(("enter", "open"), ("c", "copy value"), ("esc", "back")), classes="hint")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Field", key="field")
        table.add_column("Type", key="type")
        table.add_column("Value", key="value")
        table.add_column("", key="link")
        table.loading = True
        self._load()

    @work(thread=True, exclusive=True)
    def _load(self) -> None:
        db = self.app.db
        note = None
        try:
            details = db.describe(self.rel)
            where = self.where or _primary_key_values(details, self.row or {})
            values = None
            if where:
                fetched = db.fetch_row(self.rel, where)
                if fetched is not None:
                    values = dict(zip(*fetched))
                elif self.row is None:
                    raise QueryError("That record doesn't exist (any more).")
                else:
                    note = "This row isn't in the table any more; showing it as your query returned it."
            if values is None:
                values = dict(self.row or {})
                note = note or "These results don't include the primary key, so this is the row as your query returned it."
            # Only the tables we can follow: without the key's value there's nothing to look up.
            incoming = [
                fk for fk in (db.referenced_by(self.rel) if self.rel.kind != "view" else [])
                if _incoming_where(fk, values) is not None
            ]
        except Exception as exc:
            self.app.call_from_thread(self._failed, str(exc))
            return
        self.app.call_from_thread(self._show, details, values, incoming, note)
        for index, fk in enumerate(incoming):
            try:
                count = db.count_rows(Relation(fk.schema, fk.table, "table"), _incoming_where(fk, values))
            except QueryError:
                continue
            self.app.call_from_thread(self._show_count, index, count)

    def _failed(self, message: str) -> None:
        self.query_one(DataTable).loading = False
        self.query_one("#record-note", Static).update(Text(message, style="red"))

    def _show(self, details: TableDetails, values: dict[str, object], incoming: list[ForeignKey], note: str | None) -> None:
        self.values, self.incoming = values, incoming
        self.outgoing = {column: fk for fk in details.foreign_keys for column in fk.columns}
        table = self.query_one(DataTable)
        table.loading = False

        keys = [c.name for c in details.columns if c.primary_key]
        title = Text(self.rel.name, style="bold")
        if keys and all(key in values for key in keys):
            title.append("   " + ", ".join(f"{key} = {values[key]}" for key in keys), style="dim")
        self.query_one("#record-title", Label).update(title)
        self.query_one("#record-note", Static).update(Text(note or "", style="yellow"))

        types = {column.name: column.type for column in details.columns}
        primary = set(keys)
        for name, value in values.items():
            link = ""
            fk = self.outgoing.get(name)
            if fk is not None and value is not None:
                link = Text(f"→ {qualified(fk.ref_schema, fk.ref_table, self.rel.schema)}", style="cyan")
            table.add_row(Text(name, style="bold" if name in primary else ""), types.get(name, ""), cell(value), link, key=f"field:{name}")
        if incoming:
            table.add_row("", "", "", "", key="gap")
            table.add_row(Text("Related", style="bold"), "", "", "", key="heading")
            for index, fk in enumerate(incoming):
                source = qualified(fk.schema, fk.table, self.rel.schema)
                table.add_row(Text(f"← {source}", style="cyan"), ", ".join(fk.columns), Text("…", style="dim"), "", key=f"in:{index}")
        table.focus()

    def _show_count(self, index: int, count: int) -> None:
        text = Text(f"{count:,} row{'' if count == 1 else 's'}", style="" if count else "dim")
        self.query_one(DataTable).update_cell(f"in:{index}", "value", text)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value or ""
        if key.startswith("field:"):
            name = key.removeprefix("field:")
            fk = self.outgoing.get(name)
            if fk is not None and self.values.get(name) is not None:
                target = Relation(fk.ref_schema, fk.ref_table, "table")
                where = {ref: self.values.get(column) for column, ref in zip(fk.columns, fk.ref_columns)}
                self.app.push_screen(RecordScreen(target, where=where))
            else:
                self.app.push_screen(CellScreen(name, self.values.get(name)))
        elif key.startswith("in:"):
            fk = self.incoming[int(key.removeprefix("in:"))]
            self.app.query_related(Relation(fk.schema, fk.table, "table"), _incoming_where(fk, self.values))

    def action_copy(self) -> None:
        table = self.query_one(DataTable)
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or ""
        if key.startswith("field:"):
            self.app.copy_to_clipboard(full_text(self.values.get(key.removeprefix("field:"))))
            self.notify("Copied to the clipboard.", timeout=2)

    def action_back(self) -> None:
        self.dismiss()


def _primary_key_values(details: TableDetails, row: dict[str, object]) -> dict[str, object] | None:
    """The row's primary key values, matching column names without regard to case."""
    keys = [column.name for column in details.columns if column.primary_key]
    by_lower = {name.lower(): name for name in row}
    if not keys or any(key.lower() not in by_lower for key in keys):
        return None
    values = {key: row[by_lower[key.lower()]] for key in keys}
    return None if any(value is None for value in values.values()) else values


def _incoming_where(fk: ForeignKey, values: dict[str, object]) -> dict[str, object] | None:
    """The condition on fk's table that finds the rows pointing at this record."""
    where = {column: values.get(ref) for column, ref in zip(fk.columns, fk.ref_columns)}
    return None if any(value is None for value in where.values()) else where
