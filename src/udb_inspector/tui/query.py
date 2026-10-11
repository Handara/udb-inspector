"""The Query tab: an SQL editor over a results grid."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

from rich.console import Group
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option

from .. import export
from ..connections import tilde
from ..database import MAX_ROWS, QueryError, QueryResult, Relation
from ..hints import PRODUCTS, Context, Suggestion, TableRef, cheat_sheet, context_at, notes, suggest, tables_in
from ..sql import ReadOnlyError, sqlglot_dialect, statement_at, syntax_problem
from .record import RecordScreen
from .screens import ConfirmScreen, form_row, key_hints
from .values import CellScreen, cell, plain, sort_key

PAGE_SIZES = [("25 rows", "25"), ("50 rows", "50"), ("100 rows", "100"), ("250 rows", "250"), ("1000 rows", "1000"), ("All rows", "all")]


class QueryEditor(TextArea):
    """SQL editor whose tab key opens the list of suggestions."""

    BINDINGS = [
        # priority: checked before TextArea's own tab handling; when there is
        # nothing to suggest, check_action turns it off and tab indents as usual.
        Binding("tab", "open_suggestions", "Suggestions", show=False, priority=True),
        # priority too: TextArea's own escape moves focus on before bindings run.
        Binding("escape", "escape", "Hide suggestions or leave", show=False, priority=True),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.suggestions: list[Suggestion] = []
        self.context = Context("none")
        self.dismissed = False

    @property
    def can_suggest(self) -> bool:
        # Also with nothing typed yet (after SELECT , WHERE , …). Tab indents
        # where there's nothing to suggest, such as in a string or comment.
        return bool(self.suggestions) and not self.dismissed

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "open_suggestions":
            return self.can_suggest
        return super().check_action(action, parameters)

    def action_open_suggestions(self) -> None:
        self.post_message(self.OpenSuggestions())

    def insert_suggestion(self, suggestion: Suggestion) -> None:
        row, column = self.cursor_location
        self.replace(suggestion.text, (row, column - len(self.context.prefix)), (row, column))

    def action_escape(self) -> None:
        """Hide the suggestions; with none showing, leave for the results or the tables."""
        if self.suggestions and not self.dismissed:
            self.dismissed = True
            self.post_message(self.SuggestionsChanged())
            return
        panel = next(node for node in self.ancestors if isinstance(node, QueryPanel))
        if panel.result is not None and panel.result.columns:
            panel.grid.focus()
        else:
            self.app.action_focus_tables()

    class SuggestionsChanged(Message):
        pass

    class OpenSuggestions(Message):
        pass


class SuggestionList(OptionList):
    """The suggestions as a list: tab / shift+tab or arrows to move, enter to insert."""

    BINDINGS = [
        Binding("tab", "cursor_down", "Next", show=False),
        Binding("shift+tab", "cursor_up", "Previous", show=False),
        Binding("escape", "close", "Back to the editor", show=False),
    ]

    class Closed(Message):
        def __init__(self, typed: str | None = None) -> None:
            super().__init__()
            self.typed = typed  # a character typed while the list had focus

    def action_close(self) -> None:
        self.post_message(self.Closed())

    def on_key(self, event) -> None:
        if event.is_printable:  # keep typing: the letter belongs in the editor
            event.stop()
            event.prevent_default()
            self.post_message(self.Closed(event.character))

    def on_blur(self) -> None:
        self.display = False


class QueryPanel(Vertical):
    BINDINGS = [
        # ctrl+j: what some terminals send for ctrl+enter. ctrl+r and F5 for terminals
        # that send ctrl+enter as a plain enter.
        Binding("ctrl+enter,ctrl+j,ctrl+r,f5", "run", "Run"),
        Binding("f1", "cheat_sheet", "Cheat sheet"),
        Binding("ctrl+s,x", "export", "Export"),
        Binding("o", "open_record", "Open record"),
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
        self._columns: dict[Relation, list[tuple[str, str]]] = {}
        self._focus_results = False
        self._loading: set[Relation] = set()
        self._notes: list[str] = []
        self._syntax: str | None = None
        self._syntax_timer = None

    def compose(self) -> ComposeResult:
        yield QueryEditor.code_editor("", language="sql", id="editor", show_line_numbers=True)
        yield SuggestionList(id="suggestions")
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
        return key_hints(("ctrl+enter", "run the statement under the cursor"), ("f1", "cheat sheet"), ("esc", "leave the editor"))

    # Hints

    @on(TextArea.Changed, "#editor")
    @on(TextArea.SelectionChanged, "#editor")
    def _editor_changed(self, event) -> None:
        editor = self.editor
        if isinstance(event, TextArea.Changed):
            editor.dismissed = False
            self._syntax = None
            if self._syntax_timer is not None:
                self._syntax_timer.stop()
            self._syntax_timer = self.set_timer(0.6, self._check_syntax)
        self._refresh_suggestions()

    @on(QueryEditor.SuggestionsChanged)
    def _suggestions_changed(self) -> None:
        self._render_hints()

    @on(QueryEditor.OpenSuggestions)
    def _open_suggestions(self) -> None:
        listing = self.query_one(SuggestionList)
        width = max(len(item.label) for item in self.editor.suggestions)
        listing.clear_options()
        listing.add_options(
            [
                Option(Text.assemble((item.label.ljust(width), "bold"), "   ", (item.detail or item.kind, "dim")), id=str(index))
                for index, item in enumerate(self.editor.suggestions)
            ]
        )
        listing.highlighted = 0
        listing.display = True
        listing.focus()
        self._render_hints()

    @on(OptionList.OptionSelected, "#suggestions")
    def _suggestion_selected(self, event: OptionList.OptionSelected) -> None:
        suggestion = self.editor.suggestions[int(event.option.id)]
        self._close_suggestions()
        self.editor.insert_suggestion(suggestion)

    @on(SuggestionList.Closed)
    def _suggestions_closed(self, event: SuggestionList.Closed) -> None:
        self._close_suggestions()
        if event.typed:
            self.editor.insert(event.typed)

    def _close_suggestions(self) -> None:
        self.query_one(SuggestionList).display = False
        self.editor.focus()

    def _refresh_suggestions(self) -> None:
        info, db = self.app.info, self.app.db
        if info is None or db is None:
            return
        editor = self.editor
        offset = editor.document.get_index_from_location(editor.cursor_location)
        context = context_at(editor.text, offset, info.dialect)
        editor.context = context
        editor.suggestions = suggest(
            context, info.dialect, self._tables(), self._columns_for, db.engine.dialect.identifier_preparer.quote
        )[:50]
        statement = statement_at(editor.text, offset, sqlglot_dialect(info.dialect))
        self._notes = notes(statement, info.dialect)
        self._render_hints()

    def _tables(self) -> list[tuple[str, str, str]]:
        default = self.app.info.default_schema
        return [
            ("" if schema == default else schema, rel.name, rel.kind)
            for schema, relations in self.app.relations.items()
            for rel in relations
        ]

    def _columns_for(self, ref: TableRef) -> list[tuple[str, str]] | None:
        rel = self._resolve(ref)
        if rel is None:
            return []  # a CTE or a table we don't know
        if rel in self._columns:
            return self._columns[rel]
        if rel not in self._loading:
            self._loading.add(rel)
            self._load_columns(rel)
        return None

    def _resolve(self, ref: TableRef) -> Relation | None:
        schemas = [ref.schema] if ref.schema else [self.app.info.default_schema, *self.app.relations]
        for schema in schemas:
            for rel in self.app.relations.get(schema, []):
                if rel.name.lower() == ref.name.lower():
                    return rel
        return None

    @work(thread=True, group="columns")
    def _load_columns(self, rel: Relation) -> None:
        try:
            columns = self.app.db.column_names(rel)
        except Exception:
            columns = []
        self.app.call_from_thread(self._columns_loaded, rel, columns)

    def _columns_loaded(self, rel: Relation, columns: list[tuple[str, str]]) -> None:
        self._columns[rel] = columns
        self._loading.discard(rel)
        self._refresh_suggestions()

    def _check_syntax(self) -> None:
        if self.app.info is None:
            return
        editor = self.editor
        offset = editor.document.get_index_from_location(editor.cursor_location)
        statement = statement_at(editor.text, offset, sqlglot_dialect(self.app.info.dialect))
        self._syntax = syntax_problem(statement, self.app.info.dialect)
        self._render_hints()

    def _render_hints(self) -> None:
        editor = self.editor
        context = editor.context
        lines: list[Text] = []
        listing = self.query_one(SuggestionList)
        if listing.display:
            lines.append(key_hints(("↑↓ tab", "choose"), ("enter", "insert"), ("esc", "back")))
        elif editor.suggestions and not editor.dismissed:
            heading = {
                "table": "Tables",
                "statement": "Start with",
            }.get(context.kind, f"Columns of {context.qualifier}" if context.qualifier else "Suggestions")
            line = Text(f"{heading}:  ", style="dim", no_wrap=True, overflow="ellipsis")
            for index, item in enumerate(editor.suggestions[:8]):
                if index:
                    line.append("  |  ", style="dim")
                line.append(item.label, style="bold" if index == 0 else "")
                if item.detail or item.kind in ("table", "view") and context.kind != "table":
                    line.append(f" {item.detail or item.kind}", style="dim")
            if len(editor.suggestions) > 8:
                line.append(f"  +{len(editor.suggestions) - 8} more", style="dim")
            lines.append(line)
        elif context.kind == "column" and not context.scope and editor.text.strip():
            lines.append(Text("Name a table after FROM to get its columns.", style="dim"))
        if self._notes:
            lines.append(Text("⚠ " + self._notes[0], style="yellow"))
        elif self._syntax:
            lines.append(Text("⚠ " + self._syntax, style="yellow"))
        if editor.can_suggest and not listing.display:
            lines.append(key_hints(("tab", "pick a suggestion"), ("esc", "hide"), ("ctrl+enter", "run"), ("f1", "cheat sheet")))
        if not lines:
            lines.append(self._default_hints())
        self.query_one("#hints", Static).update(Group(*lines[:2]))  # a Group keeps each line's no_wrap

    def action_cheat_sheet(self) -> None:
        if self.app.info is not None:
            self.app.push_screen(CheatSheetScreen(self.app.info.dialect))

    def reset(self) -> None:
        """Forget the results and cached columns of the previous connection."""
        self.result, self.view, self.sort, self._haystack = None, [], None, None
        self._columns.clear()
        self._loading.clear()
        self.grid.clear(columns=True)
        self.query_one("#status", Static).update("")
        self.query_one("#pager", Static).update("")

    def set_sql(self, sql: str) -> None:
        self.editor.text = sql
        self.editor.move_cursor(self.editor.document.end)

    # Running

    def action_run(self, focus_results: bool = False) -> None:
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
        self._focus_results = focus_results  # a loading widget can't take focus yet
        self._run(sql)

    @work(thread=True, exclusive=True, group="query")
    def _run(self, sql: str) -> None:
        try:
            result = self.app.db.run_query(sql, max_rows=MAX_ROWS)
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
        if self._focus_results:
            self.grid.focus()
        self.refresh_bindings()

    # Export

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "export":
            return self.result is not None and bool(self.result.columns)
        if action == "open_record":
            return bool(self.view) and self.grid.has_focus
        return True

    def action_open_record(self) -> None:
        size = self.page_size or len(self.view)
        position = self.page * size + self.grid.cursor_row
        if position >= len(self.view):
            return
        row = dict(zip(self.result.columns, self.result.rows[self.view[position]]))
        refs = tables_in(self.result.sql, sqlglot_dialect(self.app.info.dialect))
        if len(refs) != 1:
            reads = "reads several tables" if refs else "doesn't read a table"
            self._set_status(Text(f"Records open from a query on one table; this one {reads}.", style="yellow"))
            return
        rel = self._resolve(refs[0])
        if rel is None:
            self._set_status(Text(f"{refs[0].name} isn't a table in the sidebar.", style="yellow"))
            return
        self.app.push_screen(RecordScreen(rel, row=row))

    @on(DataTable.CellHighlighted, "#grid")
    @on(DataTable.CellSelected, "#grid")
    def _grid_moved(self) -> None:
        self.refresh_bindings()

    def action_export(self) -> None:
        result = self.result
        refs = tables_in(result.sql, sqlglot_dialect(self.app.info.dialect))
        reordered = self.sort is not None or self.query_one("#search", Input).value.strip()
        self.app.push_screen(
            ExportScreen(
                directory=self.app.cwd,
                base=refs[0].name if refs else "query",
                shown=len(self.view),
                choose_rows=bool(result.truncated or reordered),
            ),
            self._start_export,
        )

    def _start_export(self, request: ExportRequest | None) -> None:
        if request is None:
            return
        rows = [self.result.rows[index] for index in self.view]
        if request.fmt == "clipboard":
            self.app.copy_to_clipboard(export.tab_separated(self.result.columns, rows))
            self._set_status(Text(f"Copied {len(rows):,} rows to the clipboard.", style="green"))
            return
        self._set_status(Text("Exporting…", style="dim"))
        self._export(request, rows if request.scope == "view" else None)

    @work(thread=True, exclusive=True, group="export")
    def _export(self, request: ExportRequest, rows: list[tuple] | None) -> None:
        def progress(count: int) -> None:
            self.app.call_from_thread(self._set_status, Text(f"Exporting… {count:,} rows", style="dim"))

        try:
            if rows is not None:  # the rows on screen, after search and sort
                batches = (rows[start : start + 1000] for start in range(0, len(rows), 1000))
                report = export.write(request.fmt, request.path, self.result.columns, batches, progress)
            else:  # everything: run the query again and stream it
                with self.app.db.stream_query(self.result.sql) as (columns, batches):
                    report = export.write(request.fmt, request.path, columns, batches, progress)
        except (QueryError, ReadOnlyError, OSError) as exc:
            self.app.call_from_thread(self._set_status, Text(f"Export failed: {exc}", style="red"))
            return
        self.app.call_from_thread(self._exported, request, report)

    def _exported(self, request: ExportRequest, report: export.ExportReport) -> None:
        message = f"Exported {report.rows:,} rows to {tilde(request.path)}"
        self._set_status(Text(message + (f". {report.note}" if report.note else ""), style="yellow" if report.note else "green"))
        self.notify(message, title="Export finished", timeout=6)

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
                self._haystack = ["\x00".join(plain(value).lower() for value in row) for row in rows]
            self.view = [index for index, text in enumerate(self._haystack) if needle in text]
        else:
            self.view = list(range(len(rows)))
        if self.sort is not None:
            column, descending = self.sort
            values = [index for index in self.view if rows[index][column] is not None]
            nulls = [index for index in self.view if rows[index][column] is None]
            values.sort(key=lambda index: sort_key(rows[index][column]), reverse=descending)
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
        grid.add_rows([[cell(value) for value in rows[index]] for index in shown])
        self._set_status(self._summary())
        if self.view:
            position = f"Rows {start + 1}–{start + len(shown)} of {len(self.view)}  |  page {self.page + 1} of {pages}"
        else:
            position = "No rows"
        pager = Text(position + "    ", style="dim")
        pager.append_text(key_hints(("o", "open record"), ("enter", "full value"), ("[ ]", "page"), ("/", "search"), ("click a header", "sort")))
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


@dataclass(frozen=True)
class ExportRequest:
    fmt: str  # a key of export.FORMATS
    scope: str  # "all": run the query again; "view": the rows on screen
    path: Path | None


class ExportScreen(ModalScreen[ExportRequest | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "export", "Export")]
    AUTO_FOCUS = "#format"

    def __init__(self, directory: Path, base: str, shown: int, choose_rows: bool):
        super().__init__()
        self.directory = directory
        self.stem = f"{base}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
        self.shown = shown
        self.choose_rows = choose_rows
        self.suggested = self.stem + ".csv"

    def compose(self) -> ComposeResult:
        scopes = [("All rows (runs the query again)", "all"), (f"The {self.shown:,} rows shown, after search and sort", "view")]
        with Vertical(classes="dialog form"):
            yield Label("Export results", classes="title")
            yield form_row("Format", Select([(label, key) for key, label in export.FORMATS.items()], value="csv", allow_blank=False, id="format", compact=True))
            yield form_row("Rows", Select(scopes, value="all", allow_blank=False, id="scope", compact=True), id="row-scope")
            yield form_row("File", Input(self.suggested, id="file", compact=True), id="row-file")
            yield Label(f"Relative names are saved in {tilde(self.directory)}.", classes="hint", markup=False)
            yield Static(id="export-error", classes="error", markup=False)
            yield Label(key_hints(("ctrl+s", "export"), ("esc", "cancel")), classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Export", id="export", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#row-scope").display = self.choose_rows

    @on(Select.Changed, "#format")
    def _format_changed(self, event: Select.Changed) -> None:
        clipboard = event.value == "clipboard"
        self.query_one("#row-file").display = not clipboard
        self.query_one("#row-scope").display = self.choose_rows and not clipboard
        file = self.query_one("#file", Input)
        if not clipboard and file.value == self.suggested:  # only rename what we suggested
            self.suggested = f"{self.stem}.{event.value}"
            file.value = self.suggested

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_export() if event.button.id == "export" else self.action_cancel()

    def action_export(self) -> None:
        fmt = self.query_one("#format", Select).value
        scope = self.query_one("#scope", Select).value if self.choose_rows else "view"
        if fmt == "clipboard":
            self.dismiss(ExportRequest(fmt, "view", None))
            return
        name = self.query_one("#file", Input).value.strip()
        if not name:
            self.query_one("#export-error", Static).update("Give the file a name.")
            return
        path = Path(name).expanduser()
        path = path if path.is_absolute() else self.directory / path
        if not path.parent.is_dir():
            self.query_one("#export-error", Static).update(f"{tilde(path.parent)} doesn't exist.")
            return
        request = ExportRequest(fmt, scope, path)
        if path.exists():
            self.app.push_screen(ConfirmScreen(f"{tilde(path)} exists. Replace it?"), lambda yes: yes and self.dismiss(request))
        else:
            self.dismiss(request)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CheatSheetScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,f1", "close", "Close")]

    def __init__(self, dialect: str):
        super().__init__()
        self.dialect = dialect

    def compose(self) -> ComposeResult:
        from rich.table import Table

        table = Table(box=None, show_header=False, padding=(0, 2, 0, 0))
        table.add_column(style="dim")
        table.add_column(style="bold")
        for topic, snippet in cheat_sheet(self.dialect):
            table.add_row(topic, snippet)
        with Vertical(classes="dialog"):
            yield Label(f"{PRODUCTS.get(self.dialect, self.dialect)} cheat sheet", classes="title", markup=False)
            yield Static(table)
            yield Label(key_hints(("esc", "close")), classes="hint")

    def action_close(self) -> None:
        self.dismiss()
