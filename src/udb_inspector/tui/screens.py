"""Dialogs for choosing, adding and unlocking connections."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from ..connection_setup import (
    DEFAULT_PORTS,
    NAME_PATTERN,
    Guess,
    Saved,
    config_path,
    delete_connection,
    guess_connections,
    load_or_new,
    save_connection,
    saved_connections,
    secret_variable,
    set_allow_writes,
    validate,
)
from ..connections import FILE_TYPES, Config, ConfigError, connection_target, find_project_config, read_env_file, tilde
from ..database import ConnectError, open_database
from ..detect import DetectionError, Target
from ..salesforce import SfError, SfOrg, list_orgs, sf_installed
from ..settings import save_setting

TYPE_OPTIONS = [
    ("URL", "url"),
    ("PostgreSQL", "postgres"),
    ("MySQL", "mysql"),
    ("MariaDB", "mariadb"),
    ("SQL Server", "mssql"),
    ("Oracle", "oracle"),
    ("SQLite file", "sqlite"),
    ("DuckDB file", "duckdb"),
]
SERVER_FIELDS = ["host", "port", "database", "user", "password"]


def key_hints(*pairs: tuple[str, str]) -> Text:
    """'enter open | esc close', with the keys in bold."""
    text = Text()
    for index, (key, action) in enumerate(pairs):
        if index:
            text.append("  |  ", style="dim")
        text.append(key, style="bold")
        text.append(f" {action}")
    return text


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog small"):
            yield Label(self.question, markup=False)
            yield Label(key_hints(("y", "yes"), ("n", "no")), classes="hint")

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


class PasswordScreen(ModalScreen[str | None]):
    AUTO_FOCUS = "#password"  # screens otherwise inherit the app's "#tree"
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, target: Target, error: str | None = None):
        super().__init__()
        self.target = target
        self.error = error

    def compose(self) -> ComposeResult:
        url = self.target.url
        with Vertical(classes="dialog small"):
            yield Label(f"Password for {url.username or 'user'}@{url.host or 'localhost'}", classes="title", markup=False)
            yield Label(self.target.source, classes="hint", markup=False)
            if self.error:
                yield Label(self.error, classes="error", markup=False)
            yield Input(password=True, id="password")
            yield Label(key_hints(("enter", "connect"), ("esc", "cancel")), classes="hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConnectionsScreen(ModalScreen[Saved | None]):
    """Saved connections from the project and global configs."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("a", "add", "Add"),
        Binding("d", "delete", "Delete"),
        Binding("w", "toggle_writes", "Allow writes"),
        Binding("q", "app.quit", "Quit"),  # app-wide keys don't reach dialogs
    ]

    def __init__(self, cwd: Path):
        super().__init__()
        self.cwd = cwd
        self.saved: list[Saved] = []
        # Orgs the Salesforce CLI is logged in to: None while sf answers, [] without sf.
        self.sf_orgs: list[SfOrg] | None = None if sf_installed() else []
        self.sf_error: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Connections", classes="title")
            yield OptionList(id="saved")
            yield Static(id="errors", classes="error", markup=False)
            yield Label(
                key_hints(("enter", "open"), ("a", "add"), ("d", "delete"), ("w", "allow writes"), ("esc", "close"), ("q", "quit")),
                classes="hint",
            )

    def on_mount(self) -> None:
        self.refresh_list()
        if self.sf_orgs is None:
            self._load_orgs()

    @work(thread=True, exclusive=True)
    def _load_orgs(self) -> None:
        try:
            orgs, error = list_orgs(), None
        except SfError as exc:
            orgs, error = [], str(exc)
        self.app.call_from_thread(self._orgs_loaded, orgs, error)

    def _orgs_loaded(self, orgs: list[SfOrg], error: str | None) -> None:
        self.sf_orgs, self.sf_error = orgs, error
        option_list = self.query_one(OptionList)
        highlighted = option_list.highlighted
        keep = option_list.get_option_at_index(highlighted).id if highlighted is not None else None
        self.refresh_list(keep=keep)

    def refresh_list(self, keep: str | None = None) -> None:
        self.saved, errors = saved_connections(self.cwd)
        width = max((len(saved.name) for saved in self.saved), default=0)
        options: list[Option] = []
        heading = None
        for index, saved in enumerate(self.saved):
            if saved.config.label != heading:
                heading = saved.config.label
                title = "Project" if saved.config.scope == "project" else "Global"
                options.append(Option(Text.assemble((title, "bold"), f"  {tilde(saved.config.path)}", style="dim"), disabled=True))
            label = Text.assemble(("  " + saved.name.ljust(width), "bold"), "   ", (saved.summary, "dim"))
            if saved.config.connections[saved.name].get("allow_writes"):
                label.append("   writes allowed", style="yellow")
            options.append(Option(label, id=f"saved-{index}"))
        if not self.saved:
            options.append(Option(Text("No saved connections yet.", style="dim italic"), disabled=True))
        options += self._salesforce_options()
        options.append(Option(Text("+ Add a connection", style="italic"), id="add"))

        option_list = self.query_one(OptionList)
        option_list.clear_options()
        option_list.add_options(options)
        ids = [option.id for option in options]
        first = next(i for i, option in enumerate(options) if not option.disabled)
        option_list.highlighted = ids.index(keep) if keep is not None and keep in ids else first
        option_list.focus()
        self.query_one("#errors", Static).update("\n".join(errors + ([self.sf_error] if self.sf_error else [])))

    def _salesforce_options(self) -> list[Option]:
        if self.sf_orgs == [] and not self.sf_error:
            return []  # no sf, or no logins: nothing to say
        options = [Option(Text.assemble(("Salesforce", "bold"), "  orgs logged in with the sf CLI", style="dim"), disabled=True)]
        if self.sf_orgs is None:
            return options + [Option(Text("  asking sf…", style="dim italic"), disabled=True)]
        width = max((len(org.name) for org in self.sf_orgs), default=0)
        for index, org in enumerate(self.sf_orgs):
            label = Text.assemble(("  " + org.name.ljust(width), "bold"), "   ")
            label.append(org.kind, style="bold red" if org.kind == "production" else "green")
            details = [org.username] if org.alias else []
            details.append(org.instance_url.removeprefix("https://"))
            label.append("   " + "  ".join(details), style="dim")
            if org.default:
                label.append("   default", style="cyan")
            options.append(Option(label, id=f"sf-{index}"))
        return options

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == "add":
            self.action_add()
        elif event.option.id and event.option.id.startswith("sf-"):
            self.dismiss(self.sf_orgs[int(event.option.id.removeprefix("sf-"))])
        elif event.option.id:
            self.dismiss(self.saved[int(event.option.id.removeprefix("saved-"))])

    def action_add(self) -> None:
        self.app.push_screen(ConnectionForm(self.cwd), self._added)

    def _added(self, saved: Saved | None) -> None:
        if saved is not None:
            self.dismiss(saved)

    def _highlighted(self) -> tuple[str, Saved] | None:
        option_list = self.query_one(OptionList)
        if option_list.highlighted is None:
            return None
        option_id = option_list.get_option_at_index(option_list.highlighted).id or ""
        if not option_id.startswith("saved-"):
            return None
        return option_id, self.saved[int(option_id.removeprefix("saved-"))]

    def action_toggle_writes(self) -> None:
        option_list = self.query_one(OptionList)
        if option_list.highlighted is not None:
            option_id = option_list.get_option_at_index(option_list.highlighted).id or ""
            if option_id.startswith("sf-"):
                self._save_org_with_writes(self.sf_orgs[int(option_id.removeprefix("sf-"))])
                return
        highlighted = self._highlighted()
        if highlighted is None:
            return
        option_id, saved = highlighted
        if saved.config.connections[saved.name].get("allow_writes"):
            set_allow_writes(saved.config, saved.name, False)
            self.refresh_list(keep=option_id)
            return

        def confirmed(yes: bool | None) -> None:
            if yes:
                set_allow_writes(saved.config, saved.name, True)
                self.refresh_list(keep=option_id)

        question = f"Allow writes to {saved.name}? The Import tab will be able to insert, update and delete its rows."
        self.app.push_screen(ConfirmScreen(question), confirmed)

    def _save_org_with_writes(self, org: SfOrg) -> None:
        """Writes are a setting of saved connections, so the org becomes one."""
        name = org.alias if org.alias and NAME_PATTERN.match(org.alias) else "sf-" + re.sub(r"[^A-Za-z0-9_-]+", "-", org.name).strip("-")
        config = load_or_new("global", self.cwd)
        if name in config.connections:
            self.notify(f"There's already a saved connection called {name}; press w on it.", timeout=6)
            return

        def confirmed(yes: bool | None) -> None:
            if yes:
                set_allow_writes(save_connection(config, name, {"url": f"sf:{org.name}"}, {}), name, True)
                self.refresh_list()
                index = next(i for i, saved in enumerate(self.saved) if saved.name == name and saved.config.scope == "global")
                option_list = self.query_one(OptionList)
                option_list.highlighted = option_list.get_option_index(f"saved-{index}")

        warning = "This is a PRODUCTION org: imports change live data. " if org.kind == "production" else ""
        question = (
            f"{warning}Save {org.name} as the global connection {name}, with writes allowed? "
            "The Import tab will be able to insert, update and delete its records."
        )
        self.app.push_screen(ConfirmScreen(question), confirmed)

    def action_delete(self) -> None:
        highlighted = self._highlighted()
        if highlighted is None:
            return
        saved = highlighted[1]

        def confirmed(yes: bool | None) -> None:
            if yes:
                delete_connection(saved.config, saved.name)
                self.refresh_list()

        self.app.push_screen(ConfirmScreen(f"Delete {saved.name} from {saved.config.label}?"), confirmed)

    def action_close(self) -> None:
        self.dismiss(None)


class ConnectionForm(ModalScreen[Saved | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+t", "test", "Test"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, cwd: Path):
        super().__init__()
        self.cwd = cwd
        project = find_project_config(cwd)
        self.scope = "project" if project or (cwd / ".env").is_file() else "global"
        self.config: Config | None = None
        self.guesses: list[Guess] = []

    def compose(self) -> ComposeResult:
        scopes = [
            (f"This project  {tilde(config_path('project', self.cwd))}", "project"),
            (f"Global  {tilde(config_path('global', self.cwd))}", "global"),
        ]
        with VerticalScroll(classes="dialog form"):
            yield Label("Add a connection", classes="title")
            yield form_row("Name", Input(id="name", compact=True))
            yield form_row("Save in", Select(scopes, value=self.scope, allow_blank=False, id="scope", compact=True))
            yield form_row("From .env", Select([], prompt="pick to fill in the fields", id="guess", compact=True), id="row-guess")
            yield form_row("Type", Select(TYPE_OPTIONS, value="postgres", allow_blank=False, id="type", compact=True))
            yield form_row("URL", Input(id="url", compact=True), id="row-url")
            for key in SERVER_FIELDS:
                yield form_row(key.capitalize(), Input(id=key, compact=True), id=f"row-{key}")
            yield Checkbox("Allow writes: the Import tab may change this database", id="allow-writes", compact=True)
            yield Label("Type values in, or write ${VAR} to read them from the env file.", classes="hint", markup=False)
            yield Label(key_hints(("ctrl+t", "test"), ("ctrl+s", "save"), ("esc", "cancel")), classes="hint")
            yield Static(id="status", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Test", id="test")
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._scope_changed()
        self._type_changed("postgres")
        self.query_one("#name", Input).focus()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "scope" and event.value != self.scope:
            self.scope = event.value
            self._scope_changed()
        elif event.select.id == "type":
            self._type_changed(event.value)
        elif event.select.id == "guess" and isinstance(event.value, str):
            self._fill(self.guesses[int(event.value)])

    def _scope_changed(self) -> None:
        self._status("")
        try:
            self.config = load_or_new(self.scope, self.cwd)
            env = read_env_file(self.config.env_file, private=self.scope == "global")
        except ConfigError as exc:
            self._status(str(exc), error=True)
            env = {}
        self.guesses = guess_connections(env)
        guess = self.query_one("#guess", Select)
        # String values: Select's "nothing chosen" marker compares equal to 0.
        guess.set_options([(g.summary, str(index)) for index, g in enumerate(self.guesses)])
        self.query_one("#row-guess").display = bool(self.guesses)

        name = self.query_one("#name", Input)
        if name.value in ("", "default"):
            taken = self.config is not None and "default" in self.config.connections
            name.value = "" if taken else "default"

        password = self.query_one("#password", Input)
        password.password = self.scope == "global"
        if self.scope == "global":
            password.placeholder = f"saved to {tilde(self.config.env_file) if self.config else '.env'}; empty: asked when connecting"
        else:
            password.placeholder = "${DB_PASSWORD} from .env; empty: asked when connecting"

    def _type_changed(self, type_: str) -> None:
        self.query_one("#row-url").display = type_ == "url"
        for key in SERVER_FIELDS:
            shown = type_ not in ("url", *FILE_TYPES) or (key == "database" and type_ in FILE_TYPES)
            self.query_one(f"#row-{key}").display = shown
        self.query_one("#row-database Label", Label).update("File" if type_ in FILE_TYPES else "Database")
        port = self.query_one("#port", Input)
        if type_ in DEFAULT_PORTS and port.value in ("", *DEFAULT_PORTS.values()):
            port.value = DEFAULT_PORTS[type_]

    def _fill(self, guess: Guess) -> None:
        fields = guess.fields
        if "url" in fields:
            self.query_one("#type", Select).value = "url"
            self.query_one("#url", Input).value = fields["url"]
            return
        if "type" in fields:
            self.query_one("#type", Select).value = fields["type"]
        for key in SERVER_FIELDS:
            self.query_one(f"#{key}", Input).value = fields.get(key, "")

    def _fields(self) -> tuple[dict[str, object], dict[str, str]]:
        """Config entries, and secrets to put in the env file."""
        fields, secrets = self._connection_fields()
        if self.query_one("#allow-writes", Checkbox).value:
            fields["allow_writes"] = True
        return fields, secrets

    def _connection_fields(self) -> tuple[dict[str, object], dict[str, str]]:
        value = lambda key: self.query_one(f"#{key}", Input).value.strip()  # noqa: E731
        type_ = self.query_one("#type", Select).value
        if type_ == "url":
            return {"url": value("url")}, {}
        if type_ in FILE_TYPES:
            return {"type": type_, "database": value("database")}, {}
        fields = {"type": type_, **{key: value(key) for key in SERVER_FIELDS[:-1] if value(key)}}
        password, secrets = self.query_one("#password", Input).value, {}
        if password and self.scope == "global" and "${" not in password:
            variable = secret_variable(value("name") or "connection")
            secrets[variable] = password
            password = f"${{{variable}}}"
        if password:
            fields["password"] = password
        return fields, secrets

    def on_button_pressed(self, event: Button.Pressed) -> None:
        {"test": self.action_test, "save": self.action_save, "cancel": self.action_cancel}[event.button.id]()

    def action_test(self) -> None:
        if self.config is None:
            return
        fields, secrets = self._fields()
        config = replace(self.config, _env={**self.config.env(), **secrets}) if secrets else self.config
        try:
            target = connection_target(fields, config, "test")
        except DetectionError as exc:
            self._status(str(exc), error=True)
            return
        self._status("Connecting…")
        self._test(target)

    @work(thread=True, exclusive=True)
    def _test(self, target: Target) -> None:
        try:
            db = open_database(target)
            try:
                info = db.server_info()
            finally:
                db.close()
        except ConnectError as exc:
            self.app.call_from_thread(self._status, str(exc), True)
            return
        self.app.call_from_thread(self._status, f"Connected: {info.product} {info.version}")

    def action_save(self) -> None:
        if self.config is None:
            return
        name = self.query_one("#name", Input).value.strip()
        fields, secrets = self._fields()
        problem = validate(self.config, name, fields)
        if problem:
            self._status(problem, error=True)
            return
        try:
            config = save_connection(self.config, name, fields, secrets)
        except (ConfigError, OSError) as exc:
            self._status(str(exc), error=True)
            return
        self.dismiss(Saved(config, name))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _status(self, message: str, error: bool = False) -> None:
        style = "red" if error else ("green" if message.startswith("Connected") else "")
        self.query_one("#status", Static).update(Text(message, style=style))


def form_row(label: str, widget, id: str | None = None) -> Horizontal:
    return Horizontal(Label(label, classes="field-label"), widget, classes="row", id=id)


class HoverOptionList(OptionList):
    """An option list where the mouse moves the highlight, not just a hover tint."""

    def on_mouse_move(self, event) -> None:
        index = event.style.meta.get("option")
        if index is not None and index != self.highlighted:
            self.highlighted = index


class ThemeScreen(ModalScreen[None]):
    """Pick a theme; the highlighted one is previewed live."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self) -> None:
        super().__init__()
        self.original = self.app.theme
        self.names = sorted(self.app.available_themes)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog small"):
            yield Label("Theme", classes="title")
            yield HoverOptionList(*[Option(name, id=name) for name in self.names], id="themes")
            yield Label(key_hints(("↑↓", "preview"), ("enter", "keep"), ("esc", "cancel")), classes="hint")

    def on_mount(self) -> None:
        themes = self.query_one(HoverOptionList)
        if self.original in self.names:
            themes.highlighted = self.names.index(self.original)
        themes.focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.app.theme = event.option.id

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.app.theme = event.option.id
        save_setting("theme", event.option.id)
        self.dismiss()

    def action_cancel(self) -> None:
        self.app.theme = self.original
        self.dismiss()
