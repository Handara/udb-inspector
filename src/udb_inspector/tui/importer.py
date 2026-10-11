"""The Import tab: paste rows from a spreadsheet, check them, write them."""

from __future__ import annotations

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Label, Select, Static, TextArea

from ..connections import tilde
from ..database import ImportReport, QueryError, Relation, RowFailure, WritesNotAllowed
from ..importing import OPERATIONS, ColumnInfo, Plan, parse_pasted, plan, save_template, template_columns, template_tsv
from .screens import ConfirmScreen
from .values import cell

PREVIEW_ROWS = 500  # the preview shows this many; every row is still imported


class ImportPanel(Vertical):
    BINDINGS = [Binding("ctrl+enter,ctrl+j,ctrl+r,f5", "run", "Import")]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.relations: list[Relation] = []
        self.columns: dict[Relation, list[ColumnInfo]] = {}
        self.plan: Plan | None = None
        self.report: ImportReport | None = None
        self._timer = None

    def compose(self) -> ComposeResult:
        with Horizontal(classes="import-bar"):
            yield Label("Table", classes="import-label")
            yield Select([], prompt="pick a table", id="import-table", compact=True)
            yield Label("Do", classes="import-label")
            yield Select([(label, key) for key, label in OPERATIONS.items()], value="insert", allow_blank=False, id="import-op", compact=True)
            yield Label("Key", classes="import-label", id="import-key-label")
            yield Select([], prompt="key", id="import-key", compact=True)
        with Horizontal(classes="import-bar"):
            yield Button("Copy template", id="copy-template", compact=True)
            yield Button("Save template as .xlsx", id="save-template", compact=True)
            yield Static(id="import-lock")
        yield TextArea(
            "",
            id="paste",
            placeholder="Paste rows copied from Excel here (ctrl+v). The first row must be the column names: copy a template to start.",
        )
        yield Static(id="import-summary")
        yield DataTable(id="import-preview", zebra_stripes=True)
        with Horizontal(classes="import-bar"):
            yield Button("Import", id="run-import", variant="warning", compact=True, disabled=True)
            yield Static(id="import-result")
        yield DataTable(id="import-failures", zebra_stripes=True)
        yield Button("Copy failed rows (with their errors)", id="copy-failed", compact=True)

    def on_mount(self) -> None:
        self.query_one("#import-failures").display = False
        self.query_one("#copy-failed").display = False

    # Choosing what to import into

    def reset(self) -> None:
        """A new connection: forget tables, columns and the last report."""
        self.relations, self.columns, self.plan, self.report = [], {}, None, None
        self.query_one("#import-table", Select).set_options([])
        self._show_report()
        self._refresh()

    def set_relations(self, relations: list[Relation], default_schema: str | None) -> None:
        tables = [rel for rel in relations if rel.kind == "table"]
        if tables == self.relations:
            return
        self.relations = tables
        label = lambda rel: rel.name if rel.schema == default_schema else f"{rel.schema}.{rel.name}"  # noqa: E731
        self.query_one("#import-table", Select).set_options([(label(rel), str(index)) for index, rel in enumerate(tables)])

    def choose(self, rel: Relation) -> None:
        if rel in self.relations:
            self.query_one("#import-table", Select).value = str(self.relations.index(rel))

    @property
    def relation(self) -> Relation | None:
        value = self.query_one("#import-table", Select).value
        return self.relations[int(value)] if isinstance(value, str) else None

    @property
    def operation(self) -> str:
        return self.query_one("#import-op", Select).value

    @property
    def key(self) -> list[str]:
        value = self.query_one("#import-key", Select).value
        return value.split(",") if isinstance(value, str) else []

    @on(Select.Changed, "#import-table")
    def _table_changed(self) -> None:
        rel = self.relation
        if rel is not None and rel not in self.columns:
            self._load_columns(rel)
        self._refresh()

    @work(thread=True, exclusive=True, group="import-columns")
    def _load_columns(self, rel: Relation) -> None:
        try:
            columns = self.app.db.import_columns(rel)
        except Exception as exc:
            self.app.call_from_thread(self._set_summary, Text(str(exc), style="red"))
            return
        self.app.call_from_thread(self._columns_loaded, rel, columns)

    def _columns_loaded(self, rel: Relation, columns: list[ColumnInfo]) -> None:
        self.columns[rel] = columns
        self._refresh()

    @on(Select.Changed, "#import-op")
    @on(Select.Changed, "#import-key")
    def _choice_changed(self) -> None:
        self._refresh()

    @on(TextArea.Changed, "#paste")
    def _pasted(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(0.3, self._refresh)  # big pastes arrive in pieces

    def _refresh(self) -> None:
        rel = self.relation
        columns = self.columns.get(rel) if rel else None
        self._update_keys(columns or [])
        lock = self.query_one("#import-lock", Static)
        writable = self.app.db is not None and self.app.db.target.allow_writes
        lock.update(
            Text("") if writable else Text("Read-only connection: to import, press c, then w on it (or start udbi with --allow-writes).", style="yellow")
        )
        self.query_one("#import-key-label").display = self.query_one("#import-key").display = self.operation != "insert"
        if columns is None:
            self.plan = None
            self._set_summary(Text("Pick the table to import into." if rel is None else "Reading the table's columns…", style="dim"))
            self._render_preview()
            return
        pasted = parse_pasted(self.query_one("#paste", TextArea).text)
        self.plan = plan(pasted, columns, self.operation, self.key) if pasted.headers else None
        self._set_summary(self._summary(pasted.headers))
        self._render_preview()

    def _update_keys(self, columns: list[ColumnInfo]) -> None:
        select = self.query_one("#import-key", Select)
        primary = [c.name for c in columns if c.primary_key]
        options = ([(", ".join(primary) + "  (primary key)", ",".join(primary))] if primary else []) + [
            (c.name, c.name) for c in columns if [c.name] != primary
        ]
        if [value for _, value in options] != [value for _, value in getattr(self, "_key_options", [])]:
            self._key_options = options
            select.set_options(options)
            if options:
                select.value = options[0][1]

    # Preview

    def _summary(self, headers: list[str]) -> Text:
        if not headers:
            return Text("Paste rows to see how they'll be imported.", style="dim")
        result = self.plan
        if result.problems:
            return Text("⚠ " + " ".join(result.problems), style="yellow")
        parts = [f"{len(result.rows):,} rows", f"columns: {', '.join(result.mapping.values())}"]
        if result.ignored:
            parts.append(f"ignored (no such column): {', '.join(result.ignored)}")
        text = Text("  |  ".join(parts))
        if result.rejected:
            count = len(result.rejected)
            text.append(f"\n⚠ {count:,} row{' has a problem' if count == 1 else 's have problems'} and will be skipped.", style="yellow")
        return text

    def _render_preview(self) -> None:
        grid = self.query_one("#import-preview", DataTable)
        grid.clear(columns=True)
        result = self.plan
        button = self.query_one("#run-import", Button)
        writable = self.app.db is not None and self.app.db.target.allow_writes
        if result is None or result.problems:
            button.disabled, button.label = True, "Import"
            return
        names = list(result.mapping.values())
        grid.add_column("row", key="row")
        for name in names:
            grid.add_column(Text(name, style="bold"), key=name)
        grid.add_column("problem", key="problem")
        for row in result.rows[:PREVIEW_ROWS]:
            if row.values is None:
                cells = [Text(c, style="dim") for c in (row.cells + [""] * len(names))[: len(names)]]
                grid.add_row(str(row.line), *cells, Text(row.problem, style="red"))
            else:
                grid.add_row(str(row.line), *[cell(row.values[name]) if name in row.values else Text("default", style="dim italic") for name in names], "")
        ready = len(result.ready)
        button.label = f"{self.operation.capitalize()} {ready:,} row{'' if ready == 1 else 's'}"
        button.disabled = not (writable and ready)

    def _set_summary(self, text: Text) -> None:
        self.query_one("#import-summary", Static).update(text)

    # Templates

    @on(Button.Pressed, "#copy-template")
    def _copy_template(self) -> None:
        columns = self._template_columns()
        if columns:
            self.app.copy_to_clipboard(template_tsv(columns))
            self.notify("Copied the column names. Paste them into the first row in Excel.", timeout=5)

    @on(Button.Pressed, "#save-template")
    def _save_template(self) -> None:
        columns = self._template_columns()
        if not columns:
            return
        path = self.app.cwd / f"{self.relation.name}-{self.operation}-template.xlsx"

        def save(yes: bool | None = True) -> None:
            if yes:
                save_template(path, columns)
                self.notify(f"Saved {tilde(path)}. Hover a header cell in Excel to see what the column takes.", timeout=8)

        if path.exists():
            self.app.push_screen(ConfirmScreen(f"{tilde(path)} exists. Replace it?"), save)
        else:
            save()

    def _template_columns(self) -> list[ColumnInfo]:
        rel = self.relation
        if rel is None or rel not in self.columns:
            self.notify("Pick a table first.", timeout=3)
            return []
        return template_columns(self.columns[rel], self.operation, self.key)

    # Importing

    @on(Button.Pressed, "#run-import")
    def _run_pressed(self) -> None:
        self.action_run()

    def action_run(self) -> None:
        button = self.query_one("#run-import", Button)
        if button.disabled or self.plan is None:
            return
        rel, operation, ready = self.relation, self.operation, len(self.plan.ready)
        info, target = self.app.info, self.app.db.target
        verb = {"insert": "Insert", "update": "Update", "upsert": "Upsert", "delete": "Delete"}[operation]
        into = {"insert": "into", "upsert": "into"}.get(operation, "in")
        question = (
            f"{verb} {ready:,} row{'' if ready == 1 else 's'} {into} {rel.name} on {info.product} ({target.source})? "
            "Rows that fail are skipped and listed."
        )

        def confirmed(yes: bool | None) -> None:
            if yes:
                button.disabled = True
                self.query_one("#import-result", Static).update(Text("Importing…", style="dim"))
                self._import(rel, operation, self.key, self.plan)

        self.app.push_screen(ConfirmScreen(question), confirmed)

    @work(thread=True, exclusive=True, group="import")
    def _import(self, rel: Relation, operation: str, key: list[str], planned: Plan) -> None:
        total = len(planned.ready)

        def progress(done: int) -> None:
            if done % 50 == 0 or done == total:
                self.app.call_from_thread(self.query_one("#import-result", Static).update, Text(f"Importing… {done:,} of {total:,}", style="dim"))

        try:
            report = self.app.db.write_rows(rel, operation, key, planned.ready, progress)
        except (WritesNotAllowed, QueryError) as exc:
            self.app.call_from_thread(self.query_one("#import-result", Static).update, Text(str(exc), style="red"))
            return
        except Exception as exc:  # e.g. the table can't be read
            self.app.call_from_thread(self.query_one("#import-result", Static).update, Text(f"{type(exc).__name__}: {exc}", style="red"))
            return
        self.app.call_from_thread(self._imported, report, planned)

    def _imported(self, report: ImportReport, planned: Plan) -> None:
        # Rows rejected before writing count as failures too, so they're in the copy.
        report.failures = sorted(
            [RowFailure(row.line, row.cells, row.problem) for row in planned.rejected] + report.failures, key=lambda f: f.line
        )
        self.report = report
        self._show_report()
        self.query_one("#run-import", Button).disabled = False

    def _show_report(self) -> None:
        result = self.query_one("#import-result", Static)
        failures_table = self.query_one("#import-failures", DataTable)
        copy = self.query_one("#copy-failed", Button)
        report = self.report
        if report is None:
            result.update("")
            failures_table.display = copy.display = False
            return
        done = [f"{count:,} {word}" for count, word in ((report.inserted, "inserted"), (report.updated, "updated"), (report.deleted, "deleted")) if count]
        text = Text("  |  ".join(done) or "Nothing written", style="green" if done else "yellow")
        if report.failures:
            text.append(f"  |  {len(report.failures):,} failed", style="red")
        if report.stopped:
            text.append(f"\n{report.stopped}", style="red")
        result.update(text)
        failures_table.clear(columns=True)
        failures_table.add_columns("row", "error", "pasted")
        for failure in report.failures:
            failures_table.add_row(str(failure.line), Text(failure.error, style="red"), Text("  ".join(failure.cells), style="dim"))
        failures_table.display = copy.display = bool(report.failures)

    @on(Button.Pressed, "#copy-failed")
    def _copy_failed(self) -> None:
        if not self.report or not self.report.failures:
            return
        headers = parse_pasted(self.query_one("#paste", TextArea).text).headers
        lines = ["\t".join(headers + ["error"])]
        for failure in self.report.failures:
            cells = (failure.cells + [""] * len(headers))[: len(headers)]
            lines.append("\t".join(c.replace("\t", " ").replace("\n", " ") for c in cells + [failure.error]))
        self.app.copy_to_clipboard("\n".join(lines) + "\n")
        self.notify(
            "Copied the failed rows with an error column. Fix them in Excel and paste them back; the error column is ignored.",
            timeout=8,
        )
