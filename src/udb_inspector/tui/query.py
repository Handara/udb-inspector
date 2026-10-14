"""The Query tab: an SQL editor over a results grid."""

from __future__ import annotations

import datetime
import re
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
from ..database import MAX_ROWS, Page, QueryError, QueryResult, Relation
from ..hints import Context, Suggestion, TableRef
from ..language import SoqlLanguage, SqlLanguage, language_for
from ..sql import ReadOnlyError, statement_range_at
from .record import RecordScreen
from .screens import ConfirmScreen, form_row, key_hints
from .values import CellScreen, cell, plain, sort_key



EMPTY_SELECT_LIST = re.compile(r"\s*SELECT(\s+)FROM\b", re.IGNORECASE)


class QueryEditor(TextArea):
    """SQL editor whose tab key opens the list of suggestions."""

    BINDINGS = [
        # priority: checked before TextArea's own tab handling; when there is
        # nothing to suggest, check_action turns it off and tab indents as usual.
        Binding("tab", "open_suggestions", "Suggestions", show=False, priority=True),
        # priority too: TextArea's own escape moves focus on before bindings run.
        Binding("escape", "escape", "Hide suggestions or leave", show=False, priority=True),
        # ctrl+arrows move between panes, so words are alt+arrows here.
        Binding("alt+left", "cursor_word_left", "Word left", show=False),
        Binding("alt+right", "cursor_word_right", "Word right", show=False),
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
        if suggestion.kind in ("table", "view"):
            self._jump_to_empty_select_list()

    def _jump_to_empty_select_list(self) -> None:
        """After SELECT FROM orders, put the cursor where the columns go: SELECT | FROM orders."""
        offset = self.document.get_index_from_location(self.cursor_location)
        start, _ = statement_range_at(self.text, offset, None)
        match = EMPTY_SELECT_LIST.match(self.text, start)
        if match is None:
            return
        gap_start, gap_end = match.span(1)
        location = self.document.get_location_from_index
        # Two spaces to stand between, keeping any line break the user typed.
        self.replace("  " if "\n" not in match.group(1) else " " + match.group(1), location(gap_start), location(gap_end))
        self.move_cursor(location(gap_start + 1))

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
        self.row_offset = 0  # the first row shown (OFFSET)
        self.row_limit = 50  # rows per page (LIMIT)
        self.sent_sql = ""  # what the database was last sent for these results
        self._request = 0  # numbers each load, so an answer that arrives late is ignored
        self._paging_timer = None
        self.sort: tuple[int, bool] | None = None  # (column, descending)
        self._haystack: list[str] | None = None
        self._columns: dict[Relation, list[tuple[str, str]]] = {}
        self._focus_results = False
        self._loading: set[Relation] = set()
        self._notes: list[str] = []
        self._explanation: str | None = None
        self._syntax: str | None = None
        self._grouping: list[str] = []
        self._syntax_timer = None
        self._search_timer = None
        self.paged = False  # the result is one page of a query run page by page
        self._language: SqlLanguage | SoqlLanguage | None = None
        self.sql = ""  # the statement being shown
        self.columns: list[str] = []
        self.search = ""  # paged: the search the database applied
        self.total: int | None = None  # paged: rows in all, once counted
        self.has_more = False  # paged: rows exist after this page

    def compose(self) -> ComposeResult:
        with Horizontal(id="editor-row"):
            yield QueryEditor.code_editor("", language="sql", id="editor", show_line_numbers=True)
            with Vertical(id="paging"):
                yield Label("Rows to show", classes="paging-title")
                with Horizontal(classes="paging-row"):
                    yield Label("LIMIT", classes="paging-label")
                    yield Input(str(self.row_limit), id="limit", restrict=r"[0-9]*", compact=True)
                with Horizontal(classes="paging-row"):
                    yield Label("OFFSET", classes="paging-label")
                    yield Input("0", id="offset", restrict=r"[0-9]*", compact=True)
                with Horizontal(classes="paging-row"):
                    yield Label("Page", classes="paging-label")
                    yield Input("1", id="page-number", restrict=r"[0-9]*", compact=True)
                    yield Label("", id="page-count")
                yield Static(id="paging-note")
                yield Static(id="paging-sql")
        yield SuggestionList(id="suggestions")
        yield Static(self._default_hints(), id="hints")
        with Horizontal(id="result-bar"):
            yield Input(placeholder="Search results  ( / )", id="search", compact=True)
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
            self._syntax, self._grouping = None, []
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
        language = self.language
        context = language.context_at(editor.text, offset)
        editor.context = context
        editor.suggestions = language.suggest(context, self._tables(), self._columns_for, db.quote_identifier)[:50]
        start, end = language.statement_range_at(editor.text, offset)
        statement = editor.text[start:end]
        self._notes = language.notes(statement)
        self._explanation = language.explain(statement, offset - start)
        self._render_hints()

    @property
    def language(self) -> SqlLanguage | SoqlLanguage:
        """The connected database's query language: its SQL dialect, or SOQL."""
        dialect = self.app.info.dialect
        if self._language is None or self._language.dialect != dialect:
            self._language = language_for(dialect, self.app.db)
        return self._language

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
        statement = self.language.statement_at(editor.text, offset)
        self._syntax = self.language.syntax_problem(statement)
        self._grouping = self.language.grouping_problems(statement)
        self._render_hints()

    def _render_hints(self) -> None:
        editor = self.editor
        context = editor.context
        lines: list[Text] = []
        listing = self.query_one(SuggestionList)
        if listing.display:
            lines.append(key_hints(("↑↓ tab", "choose"), ("enter", "insert"), ("esc", "back")))
        elif editor.suggestions and not editor.dismissed:
            line = Text(f"{self.language.heading(context)}:  ", style="dim", no_wrap=True, overflow="ellipsis")
            for index, item in enumerate(editor.suggestions[:8]):
                if index:
                    line.append("  |  ", style="dim")
                line.append(item.label, style="bold" if index == 0 else "")
                if item.kind == "column" and item.detail:  # the list (tab) has the longer descriptions
                    line.append(f" {item.detail}", style="dim")
            if len(editor.suggestions) > 8:
                line.append(f"  +{len(editor.suggestions) - 8} more", style="dim")
            lines.append(line)
        elif context.kind == "column" and not context.scope and editor.text.strip():
            lines.append(Text(self.language.no_scope, style="dim"))
        warnings = self._notes + self._grouping + ([self._syntax] if self._syntax else [])
        if warnings:
            more = f"   (+{len(warnings) - 1} more)" if len(warnings) > 1 else ""
            lines.append(Text.assemble(("⚠ " + warnings[0], "yellow"), (more, "dim")))
        if self._explanation:
            lines.append(Text.assemble(("ℹ ", "cyan"), (self._explanation, "italic")))
        if editor.can_suggest and not listing.display and len(lines) < 3:
            lines.append(key_hints(("tab", "pick a suggestion"), ("esc", "hide"), ("ctrl+enter", "run"), ("f1", "cheat sheet")))
        if not lines:
            lines.append(self._default_hints())
        self.query_one("#hints", Static).update(Group(*lines[:3]))  # a Group keeps each line's no_wrap

    def action_cheat_sheet(self) -> None:
        if self.app.info is not None:
            self.app.push_screen(CheatSheetScreen(self.language))

    def reset(self) -> None:
        """Forget the results and cached columns of the previous connection."""
        self.result, self.view, self.sort, self._haystack = None, [], None, None
        self.paged, self.sql, self.columns, self.search, self.total, self.has_more = False, "", [], "", None, False
        self._columns.clear()
        self._loading.clear()
        self._language = None  # the next connection may speak another language
        self.grid.clear(columns=True)
        self.query_one("#status", Static).update("")
        self.query_one("#pager", Static).update("")
        self.sent_sql = ""
        self._sync_paging()

    def set_sql(self, sql: str) -> None:
        self.editor.text = sql
        self.editor.move_cursor(self.editor.document.end)

    # Running. A single SELECT is paged: each page, sort and search is a query of its
    # own (LIMIT/OFFSET or the database's equivalent), so nothing waits for the whole
    # result. Anything else (SHOW, PRAGMA, …) is fetched up to MAX_ROWS and paged here.

    def action_run(self, focus_results: bool = False) -> None:
        db = self.app.db
        if db is None:
            return
        editor = self.editor
        sql = editor.selected_text.strip()
        if not sql:
            offset = editor.document.get_index_from_location(editor.cursor_location)
            sql = self.language.statement_at(editor.text, offset)
        if not sql:
            self._set_status(Text("Nothing to run: write a query first.", style="dim"))
            return
        self.sql, self.sort, self.row_offset, self.search, self.total, self.has_more = sql, None, 0, "", None, False
        self.query_one("#search", Input).value = ""  # a new query starts unsearched
        self._set_status(Text("Running…", style="dim"))
        self.grid.loading = True
        self._focus_results = focus_results  # a loading widget can't take focus yet
        self._request += 1
        db.forget(sql)
        self._load(sql, True, self._page_args(), self._request)

    def _page_args(self) -> dict:
        sort = (self.columns[self.sort[0]], self.sort[1]) if self.sort is not None and self.columns else None
        return {"offset": self.row_offset, "limit": self.row_limit, "sort": sort, "search": self.search, "columns": list(self.columns)}

    def _reload(self) -> None:
        self.grid.loading = True
        self._request += 1
        self._load(self.sql, False, self._page_args(), self._request)

    @work(thread=True, exclusive=True, group="query")
    def _load(self, sql: str, first: bool, args: dict, request: int) -> None:
        db = self.app.db
        try:
            try:
                page = db.fetch_page(sql, **args)
            except QueryError:
                if not first:
                    raise
                page = None  # the rewritten query failed: run it as typed instead
            if page is None:
                self.app.call_from_thread(self._show_all, db.run_query(sql, max_rows=MAX_ROWS), request)
            else:
                self.app.call_from_thread(self._show_page, sql, page, first, request)
        except (ReadOnlyError, QueryError) as exc:
            self.app.call_from_thread(self._failed, str(exc), request)
        except Exception as exc:  # a driver error we didn't anticipate
            self.app.call_from_thread(self._failed, f"{type(exc).__name__}: {exc}", request)

    def _failed(self, message: str, request: int | None = None) -> None:
        if request is not None and request != self._request:
            return
        self.grid.loading = False
        self._set_status(Text(message, style="red"))

    def _show_page(self, sql: str, page: Page, first: bool, request: int) -> None:
        if request != self._request:
            return  # a page that was asked for before the latest one
        self.paged = True
        self.sent_sql = page.sql
        self.result = QueryResult(sql, page.columns, page.rows, False, page.elapsed)
        self.has_more = page.has_more
        self.view = list(range(len(page.rows)))
        if first:
            self.columns = page.columns
            self._count()
        self._shown()

    def _show_all(self, result: QueryResult, request: int | None = None) -> None:
        if request is not None and request != self._request:
            return
        self.paged = False
        self.sent_sql = result.sql
        self.result, self.columns, self._haystack = result, result.columns, None
        self._apply_view()
        self._shown()

    def _shown(self) -> None:
        self.grid.loading = False
        self._render_columns()
        self._render_page()
        if self._focus_results:
            self._focus_results = False
            self.grid.focus()
        self.refresh_bindings()

    def show_result(self, result: QueryResult) -> None:
        """Show an already-fetched result (used by tests and callers that ran the query)."""
        self.sql = result.sql
        self._show_all(result)

    def _count(self) -> None:
        self.total = None
        self._count_rows(self.sql, self.search, list(self.columns))

    @work(thread=True, exclusive=True, group="count")
    def _count_rows(self, sql: str, search: str, columns: list[str]) -> None:
        try:
            total = self.app.db.count_matching(sql, search=search, columns=columns)
        except Exception:
            total = None  # the pager says "more" instead
        self.app.call_from_thread(self._counted, sql, search, total)

    def _counted(self, sql: str, search: str, total: int | None) -> None:
        if (sql, search) == (self.sql, self.search):
            self.total = total
            self._render_status()

    # Export

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "export":
            return self.result is not None and bool(self.result.columns)
        if action == "open_record":
            return bool(self.view) and self.grid.has_focus
        return True

    def _row_index(self, grid_row: int) -> int | None:
        """The index in result.rows of a row of the grid."""
        position = grid_row if self.paged else self.row_offset + grid_row
        return self.view[position] if 0 <= position < len(self.view) else None

    def action_open_record(self) -> None:
        index = self._row_index(self.grid.cursor_row)
        if index is None:
            return
        row = dict(zip(self.result.columns, self.result.rows[index]))
        refs = self.language.tables_in(self.result.sql)
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
        refs = self.language.tables_in(result.sql)
        if self.paged:
            choose_rows, shown = bool(self.search or self.sort), self.total
        else:
            reordered = self.sort is not None or self.query_one("#search", Input).value.strip()
            choose_rows, shown = bool(result.truncated or reordered), len(self.view)
        self.app.push_screen(
            ExportScreen(directory=self.app.cwd, base=refs[0].name if refs else "query", shown=shown, choose_rows=choose_rows),
            self._start_export,
        )

    def _start_export(self, request: ExportRequest | None) -> None:
        if request is None:
            return
        rows = [self.result.rows[index] for index in self.view]
        if request.fmt == "clipboard" and self.paged:
            self._copy_matching(self._filtered())  # every matching row, not just this page
            return
        if request.fmt == "clipboard":
            self.app.copy_to_clipboard(export.tab_separated(self.result.columns, rows))
            self._set_status(Text(f"Copied {len(rows):,} rows to the clipboard.", style="green"))
            return
        self._set_status(Text("Exporting…", style="dim"))
        if request.scope == "view" and self.paged:  # every row matching the search, in the shown order
            self._export(request, None, self._filtered())
        elif request.scope == "view":
            self._export(request, rows, None)
        else:  # everything: run the query again and stream it
            self._export(request, None, self.result.sql)

    def _filtered(self) -> str:
        args = self._page_args()
        return self.app.db.filtered_sql(self.sql, sort=args["sort"], search=self.search, columns=args["columns"])

    @work(thread=True, exclusive=True, group="export")
    def _copy_matching(self, sql: str) -> None:
        rows, more = [], False
        try:
            with self.app.db.stream_query(sql) as (columns, batches):
                for batch in batches:
                    rows += batch
                    if len(rows) > MAX_ROWS:
                        rows, more = rows[:MAX_ROWS], True
                        break
        except (QueryError, ReadOnlyError) as exc:
            self.app.call_from_thread(self._set_status, Text(f"Copy failed: {exc}", style="red"))
            return
        self.app.call_from_thread(self.app.copy_to_clipboard, export.tab_separated(columns, rows))
        note = f"Copied the first {len(rows):,} rows; export to a file for all of them." if more else f"Copied {len(rows):,} rows to the clipboard."
        self.app.call_from_thread(self._set_status, Text(note, style="yellow" if more else "green"))

    @work(thread=True, exclusive=True, group="export")
    def _export(self, request: ExportRequest, rows: list[tuple] | None, sql: str | None) -> None:
        def progress(count: int) -> None:
            self.app.call_from_thread(self._set_status, Text(f"Exporting… {count:,} rows", style="dim"))

        try:
            if rows is not None:
                batches = (rows[start : start + 1000] for start in range(0, len(rows), 1000))
                report = export.write(request.fmt, request.path, self.result.columns, batches, progress)
            else:
                with self.app.db.stream_query(sql) as (columns, batches):
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
        if not self.paged:
            self._apply_view()
            return
        if self._search_timer is not None:
            self._search_timer.stop()
        self._search_timer = self.set_timer(0.4, self._apply_search)  # one query per pause, not per key

    def _apply_search(self) -> None:
        value = self.query_one("#search", Input).value.strip()
        if not self.paged or value == self.search:
            return
        self.search, self.row_offset = value, 0
        self._reload()
        self._count()

    # LIMIT, OFFSET and page number: typing one updates the others.

    def _paging_values(self) -> tuple[str, str, str]:
        return tuple(self.query_one(f"#{name}", Input).value for name in ("limit", "offset", "page-number"))

    def _current_paging(self) -> tuple[str, str, str]:
        return str(self.row_limit), str(self.row_offset), str(self.row_offset // self.row_limit + 1)

    @on(Input.Changed, "#limit")
    @on(Input.Changed, "#offset")
    @on(Input.Changed, "#page-number")
    def _paging_typed(self, event: Input.Changed) -> None:
        if self._paging_values() == self._current_paging():
            return  # our own update, or nothing to apply
        if self._paging_timer is not None:
            self._paging_timer.stop()
        changed = event.input.id
        self._paging_timer = self.set_timer(0.6, lambda: self._apply_paging(changed))

    @on(Input.Submitted, "#limit")
    @on(Input.Submitted, "#offset")
    @on(Input.Submitted, "#page-number")
    def _paging_submitted(self, event: Input.Submitted) -> None:
        if self._paging_timer is not None:
            self._paging_timer.stop()
        self._apply_paging(event.input.id)

    def _apply_paging(self, changed: str) -> None:
        limit_text, offset_text, page_text = self._paging_values()
        limit = int(limit_text or 0)
        if not 1 <= limit <= MAX_ROWS:
            self._paging_problem(f"LIMIT goes from 1 to {MAX_ROWS:,}.")
            return
        offset = int(offset_text or 0)
        if changed == "page-number":
            page = int(page_text or 0)
            if page < 1:
                self._paging_problem("Pages start at 1.")
                return
            offset = (page - 1) * limit
        if (limit, offset) == (self.row_limit, self.row_offset):
            self._sync_paging()
            return
        self.row_limit, self.row_offset = limit, offset
        if self.result is None:
            self._sync_paging()
        elif self.paged:
            self._reload()
        else:
            self._render_page()

    def _paging_problem(self, message: str) -> None:
        self.query_one("#paging-note", Static).update(Text(message, style="red"))

    def _sync_paging(self) -> None:
        """Show the current LIMIT, OFFSET and page, and say what udbi sends and why."""
        for name, value in zip(("limit", "offset", "page-number"), self._current_paging()):
            field = self.query_one(f"#{name}", Input)
            if field.value != value:
                field.value = value
        total = self.total if self.paged else (len(self.view) if self.result is not None else None)
        pages = f"of {max(1, -(-total // self.row_limit)):,}" if total is not None else ("of …" if self.result is not None else "")
        self.query_one("#page-count", Label).update(pages)
        self.query_one("#paging-note", Static).update(self._paging_note())
        sent = self.sent_sql if len(self.sent_sql) <= 400 else self.sent_sql[:399] + "…"
        self.query_one("#paging-sql", Static).update(Text.assemble(("Sent: ", "bold"), sent) if sent else "")

    def _paging_note(self) -> Text:
        if self.app.info is None:
            return Text("")
        paged = None if self.result is None else self.paged
        note = self.language.paging_note(self.row_limit, paged, self.sql, bool(self.sort or self.search))
        if paged is False:
            note += f" udbi shows its first {MAX_ROWS:,} rows, {self.row_limit:,} at a time."
        return Text(note, style="dim")

    @on(DataTable.HeaderSelected)
    def _header_selected(self, event: DataTable.HeaderSelected) -> None:
        column = event.column_index
        descending = self.sort is not None and self.sort[0] == column and not self.sort[1]
        self.sort = (column, descending)
        if self.paged:
            self.row_offset = 0
            self._reload()  # the database sorts the whole result
        else:
            self._apply_view()

    def _apply_view(self) -> None:
        """In-memory results: filter and sort the fetched rows."""
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
        self.row_offset = 0
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
        if self.paged:
            shown = self.view
        else:
            last_page = max(0, (len(self.view) - 1) // self.row_limit * self.row_limit)
            self.row_offset = min(self.row_offset, last_page)
            shown = self.view[self.row_offset : self.row_offset + self.row_limit]
        rows = self.result.rows
        grid.add_rows([[cell(value) for value in rows[index]] for index in shown])
        self._render_status()

    def _render_status(self) -> None:
        self._sync_paging()
        if self.result is None:
            return
        self._set_status(self._summary())
        pager = Text(self._position() + "    ", style="dim")
        pager.append_text(key_hints(("o", "open record"), ("enter", "full value"), ("[ ]", "page"), ("/", "search"), ("click a header", "sort")))
        self.query_one("#pager", Static).update(pager)

    def _position(self) -> str:
        if not self.view:
            return "No rows"
        start = self.row_offset
        end = start + (len(self.view) if self.paged else len(self.view[start : start + self.row_limit]))
        total = self.total if self.paged else len(self.view)
        page = start // self.row_limit + 1
        if total is not None:
            return f"Rows {start + 1:,}–{end:,} of {total:,}  |  page {page:,} of {max(1, -(-total // self.row_limit)):,}"
        if self.has_more:
            return f"Rows {start + 1:,}–{end:,} of more (counting…)  |  page {page:,}"
        return f"Rows {start + 1:,}–{end:,} of {end:,}  |  page {page:,} of {page:,}"

    def _summary(self) -> Text:
        result = self.result
        if not result.columns:
            return Text(f"Done in {result.elapsed:.2f} s; the statement returned no rows.", style="dim")
        if self.paged:
            if self.search:
                counted = f"{self.total:,} match '{self.search}'" if self.total is not None else f"counting matches for '{self.search}'…"
            else:
                counted = f"{self.total:,} row{'' if self.total == 1 else 's'}" if self.total is not None else "counting rows…"
            return Text(f"{counted}  |  page in {result.elapsed:.2f} s", style="dim")
        parts = [f"{len(result.rows):,} row{'s' if len(result.rows) != 1 else ''}", f"{result.elapsed:.2f} s"]
        if result.truncated:
            parts.append(f"only the first {len(result.rows):,} rows were fetched")
        needle = self.query_one("#search", Input).value.strip()
        if needle:
            parts.append(f"{len(self.view):,} match '{needle}'")
        return Text("  |  ".join(parts), style="dim")

    def action_page(self, step: int) -> None:
        if self.result is None:
            return
        if self.paged:
            if (step > 0 and not self.has_more) or (step < 0 and self.row_offset == 0):
                return
            self.row_offset = max(0, self.row_offset + step * self.row_limit)
            self._reload()
        else:
            self.row_offset = max(0, self.row_offset + step * self.row_limit)
            self._render_page()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    @on(Input.Submitted, "#search")
    def _search_submitted(self) -> None:
        self._focus_results = True  # once the searched page is in: a loading grid can't take focus
        self._apply_search()  # don't wait for the pause
        if not self.grid.loading:
            self._focus_results = False
            self.grid.focus()

    @on(DataTable.CellSelected)
    def _cell_selected(self, event: DataTable.CellSelected) -> None:
        index = self._row_index(event.coordinate.row)
        if index is not None:
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

    def __init__(self, directory: Path, base: str, shown: int | None, choose_rows: bool):
        super().__init__()
        self.directory = directory
        self.stem = f"{base}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
        self.shown = shown
        self.choose_rows = choose_rows
        self.suggested = self.stem + ".csv"

    def compose(self) -> ComposeResult:
        shown = f"The {self.shown:,} rows" if self.shown is not None else "The rows"
        scopes = [("All rows (runs the query again)", "all"), (f"{shown} matching the search, in the shown order", "view")]
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

    def __init__(self, language: SqlLanguage | SoqlLanguage):
        super().__init__()
        self.language = language

    def compose(self) -> ComposeResult:
        from rich.table import Table

        table = Table(box=None, show_header=False, padding=(0, 2, 0, 0))
        table.add_column(style="dim")
        table.add_column(style="bold")
        for topic, snippet in self.language.cheat_sheet():
            table.add_row(topic, snippet)
        with Vertical(classes="dialog"):
            yield Label(f"{self.language.title} cheat sheet", classes="title", markup=False)
            yield Static(table)
            yield Label(key_hints(("esc", "close")), classes="hint")

    def action_close(self) -> None:
        self.dismiss()
