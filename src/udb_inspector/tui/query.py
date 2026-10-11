"""The Query tab: an SQL editor over a results grid."""

from __future__ import annotations

import datetime
import json
from decimal import Decimal

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Label, Select, Static, TextArea

from ..database import QueryError, QueryResult
from ..sql import ReadOnlyError, sqlglot_dialect, statement_at
from .screens import key_hints

PAGE_SIZES = [("25 rows", "25"), ("50 rows", "50"), ("100 rows", "100"), ("250 rows", "250"), ("1000 rows", "1000"), ("All rows", "all")]
CELL_WIDTH = 60  # longer values are cut in the grid; enter shows them in full


class QueryEditor(TextArea):
    pass


class QueryPanel(Vertical):
    BINDINGS = [
        Binding("ctrl+r,f5,ctrl+enter", "run", "Run"),
        Binding("left_square_bracket", "page(-1)", "Previous page", show=False),
        Binding("right_square_bracket", "page(1)", "Next page", show=False),
        Binding("slash", "focus_search", "Search results", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.result: QueryResult | None = None
        self.view: list[int] = []  # result rows after search and sort
        self.page = 0
        self.page_size: int | None = 50  # None: all rows
        self.sort: tuple[int, bool] | None = None  # (column, descending)
        self._haystack: list[str] | None = None

    def compose(self) -> ComposeResult:
        yield QueryEditor.code_editor("", language="sql", id="editor", show_line_numbers=True)
        yield Static(self._default_hints(), id="hints")
        with Horizontal(id="result-bar"):
            yield Input(placeholder="Search results  ( / )", id="search", compact=True)
            yield Select(PAGE_SIZES, value="50", allow_blank=False, id="page-size", compact=True)
        yield Static(id="status")
        yield DataTable(id="grid", zebra_stripes=True, cursor_type="cell")
        yield Static(id="pager")

    @property
    def editor(self) -> QueryEditor:
        return self.query_one(QueryEditor)

    @property
    def grid(self) -> DataTable:
        return self.query_one(DataTable)

    def _default_hints(self) -> Text:
        return key_hints(("ctrl+r", "run the statement under the cursor"), ("e", "editor"), ("esc", "leave the editor"))

    def reset(self) -> None:
        """Forget the results of the previous connection."""
        self.result, self.view, self.sort, self._haystack = None, [], None, None
        self.grid.clear(columns=True)
        self.query_one("#status", Static).update("")
        self.query_one("#pager", Static).update("")

    def set_sql(self, sql: str) -> None:
        self.editor.text = sql
        self.editor.move_cursor(self.editor.document.end)

    # Running

    def action_run(self) -> None:
        db = self.app.db
        if db is None:
            return
        editor = self.editor
        sql = editor.selected_text.strip()
        if not sql:
            offset = editor.document.get_index_from_location(editor.cursor_location)
            sql = statement_at(editor.text, offset, sqlglot_dialect(self.app.info.dialect))
        if not sql:
            self._set_status(Text("Nothing to run: write a query first.", style="dim"))
            return
        self._set_status(Text("Running…", style="dim"))
        self.grid.loading = True
        self._run(sql)

    @work(thread=True, exclusive=True, group="query")
    def _run(self, sql: str) -> None:
        try:
            result = self.app.db.run_query(sql)
        except (ReadOnlyError, QueryError) as exc:
            self.app.call_from_thread(self._failed, str(exc))
            return
        except Exception as exc:  # a driver error we didn't anticipate
            self.app.call_from_thread(self._failed, f"{type(exc).__name__}: {exc}")
            return
        self.app.call_from_thread(self.show_result, result)

    def _failed(self, message: str) -> None:
        self.grid.loading = False
        self._set_status(Text(message, style="red"))

    def show_result(self, result: QueryResult) -> None:
        self.result, self.sort, self._haystack = result, None, None
        self.grid.loading = False
        self._apply_view()

    # Search, sort and pages

    @on(Input.Changed, "#search")
    def _search_changed(self) -> None:
        self._apply_view()

    @on(Select.Changed, "#page-size")
    def _page_size_changed(self, event: Select.Changed) -> None:
        self.page_size = None if event.value == "all" else int(event.value)
        self.page = 0
        self._render_page()

    @on(DataTable.HeaderSelected)
    def _header_selected(self, event: DataTable.HeaderSelected) -> None:
        column = event.column_index
        descending = self.sort is not None and self.sort[0] == column and not self.sort[1]
        self.sort = (column, descending)
        self._apply_view()

    def _apply_view(self) -> None:
        if self.result is None:
            return
        rows = self.result.rows
        needle = self.query_one("#search", Input).value.strip().lower()
        if needle:
            if self._haystack is None:
                self._haystack = ["\x00".join(_plain(value).lower() for value in row) for row in rows]
            self.view = [index for index, text in enumerate(self._haystack) if needle in text]
        else:
            self.view = list(range(len(rows)))
        if self.sort is not None:
            column, descending = self.sort
            values = [index for index in self.view if rows[index][column] is not None]
            nulls = [index for index in self.view if rows[index][column] is None]
            values.sort(key=lambda index: _sort_key(rows[index][column]), reverse=descending)
            self.view = values + nulls  # NULLs last either way
        self.page = 0
        self._render_columns()
        self._render_page()

    def _render_columns(self) -> None:
        grid = self.grid
        grid.clear(columns=True)
        for index, name in enumerate(self.result.columns):
            arrow = ""
            if self.sort is not None and self.sort[0] == index:
                arrow = " ▼" if self.sort[1] else " ▲"
            grid.add_column(Text(f"{name}{arrow}", style="bold"), key=str(index))

    def _render_page(self) -> None:
        if self.result is None:
            return
        grid = self.grid
        grid.clear()
        size = self.page_size or max(len(self.view), 1)
        pages = max(1, -(-len(self.view) // size))
        self.page = min(max(self.page, 0), pages - 1)
        start = self.page * size
        shown = self.view[start : start + size]
        rows = self.result.rows
        grid.add_rows([[_cell(value) for value in rows[index]] for index in shown])
        self._set_status(self._summary())
        if self.view:
            position = f"Rows {start + 1}–{start + len(shown)} of {len(self.view)}  |  page {self.page + 1} of {pages}"
        else:
            position = "No rows"
        pager = Text(position + "    ", style="dim")
        pager.append_text(key_hints(("[ ]", "page"), ("/", "search"), ("enter", "full value"), ("click a header", "sort")))
        self.query_one("#pager", Static).update(pager)

    def _summary(self) -> Text:
        result = self.result
        if not result.columns:
            return Text(f"Done in {result.elapsed:.2f} s; the statement returned no rows.", style="dim")
        parts = [f"{len(result.rows):,} row{'s' if len(result.rows) != 1 else ''}", f"{result.elapsed:.2f} s"]
        if result.truncated:
            parts.append(f"only the first {len(result.rows):,} rows were fetched")
        needle = self.query_one("#search", Input).value.strip()
        if needle:
            parts.append(f"{len(self.view):,} match '{needle}'")
        return Text("  |  ".join(parts), style="dim")

    def action_page(self, step: int) -> None:
        self.page += step
        self._render_page()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    @on(Input.Submitted, "#search")
    def _search_submitted(self) -> None:
        self.grid.focus()

    @on(DataTable.CellSelected)
    def _cell_selected(self, event: DataTable.CellSelected) -> None:
        size = self.page_size or len(self.view)
        index = self.view[self.page * size + event.coordinate.row]
        column = event.coordinate.column
        self.app.push_screen(CellScreen(self.result.columns[column], self.result.rows[index][column]))

    def _set_status(self, text: Text) -> None:
        self.query_one("#status", Static).update(text)


class CellScreen(ModalScreen[None]):
    """One value in full, with JSON pretty-printed."""

    BINDINGS = [Binding("escape,enter", "close", "Close"), Binding("c", "copy", "Copy")]

    def __init__(self, column: str, value: object):
        super().__init__()
        self.column = column
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.column, classes="title", markup=False)
            with VerticalScroll(id="cell-value"):
                yield Static(_full(self.value), markup=False)
            yield Label(key_hints(("c", "copy"), ("esc", "close")), classes="hint")

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(_full(self.value))
        self.notify("Copied to the clipboard.", timeout=2)

    def action_close(self) -> None:
        self.dismiss()


def _plain(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)


def _cell(value: object) -> Text:
    if value is None:
        return Text("NULL", style="dim italic")
    text = _plain(value).replace("\r\n", "↵").replace("\n", "↵")
    if len(text) > CELL_WIDTH:
        text = text[: CELL_WIDTH - 1] + "…"
    numeric = isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    return Text(text, justify="right" if numeric else "left")


def _full(value: object) -> str:
    if isinstance(value, str) and value[:1] in "{[":
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, default=str, ensure_ascii=False)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex(" ")
    return _plain(value)


def _sort_key(value: object) -> tuple:
    """Numbers by value, everything else as text."""
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return (0, float(value), "")
    return (1, 0, _plain(value).lower())
