"""F1: pages of snippets written for the database at hand, and udbi's own keys."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from .screens import key_hints


class CheatSheetScreen(ModalScreen[None]):
    AUTO_FOCUS = "#sheet"  # so up and down scroll a long page
    BINDINGS = [
        Binding("escape,f1", "close", "Close"),
        Binding("right,tab", "turn(1)", "Next page"),
        Binding("left,shift+tab", "turn(-1)", "Previous page"),
        *[Binding(str(number), f"go({number - 1})", show=False) for number in range(1, 10)],
    ]
    # The page each language's sheet was left on, so F1 reopens it there.
    last_page: dict[str, int] = {}

    def __init__(self, language) -> None:
        super().__init__()
        self.title = language.title
        self.pages = language.cheat_sheet_pages()
        self.page = min(CheatSheetScreen.last_page.get(self.title, 0), len(self.pages) - 1)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog cheat-sheet"):
            yield Label(f"{self.title} cheat sheet", classes="title", markup=False)
            yield Static(id="sheet-tabs")
            with VerticalScroll(id="sheet"):
                yield Static(id="sheet-page")
            yield Label(key_hints(("← →  tab", "pages"), ("1–9", "go to a page"), ("↑ ↓", "scroll"), ("esc", "close")), classes="hint")

    def on_mount(self) -> None:
        self._show()

    def _show(self) -> None:
        tabs = Text()
        for index, (title, _) in enumerate(self.pages):
            label = f" {index + 1} {title} "
            tabs.append(label, style="bold reverse" if index == self.page else "dim")
            tabs.append(" ")
        self.query_one("#sheet-tabs", Static).update(tabs)

        table = Table(box=None, show_header=False, padding=(0, 2, 0, 0), expand=True)
        table.add_column(style="dim", ratio=1)
        table.add_column(style="bold", ratio=3)
        for topic, snippet in self.pages[self.page][1]:
            table.add_row(topic, Text(snippet))  # Text: snippets are full of [brackets]
        self.query_one("#sheet-page", Static).update(table)
        self.query_one("#sheet", VerticalScroll).scroll_home(animate=False)
        CheatSheetScreen.last_page[self.title] = self.page

    def action_turn(self, step: int) -> None:
        self.page = (self.page + step) % len(self.pages)
        self._show()

    def action_go(self, index: int) -> None:
        if index < len(self.pages):
            self.page = index
            self._show()

    def action_close(self) -> None:
        self.dismiss()
