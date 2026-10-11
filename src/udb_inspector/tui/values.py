"""How values look in the grid and the record view, and the full-value popup."""

from __future__ import annotations

import datetime
import json
from decimal import Decimal

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from .screens import key_hints

CELL_WIDTH = 60  # longer values are cut in tables; enter shows them in full


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
                yield Static(full_text(self.value), markup=False)
            yield Label(key_hints(("c", "copy"), ("esc", "close")), classes="hint")

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(full_text(self.value))
        self.notify("Copied to the clipboard.", timeout=2)

    def action_close(self) -> None:
        self.dismiss()


def plain(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)


def cell(value: object) -> Text:
    if value is None:
        return Text("NULL", style="dim italic")
    text = plain(value).replace("\r\n", "↵").replace("\n", "↵")
    if len(text) > CELL_WIDTH:
        text = text[: CELL_WIDTH - 1] + "…"
    numeric = isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    return Text(text, justify="right" if numeric else "left")


def full_text(value: object) -> str:
    if isinstance(value, str) and value[:1] in "{[":
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, default=str, ensure_ascii=False)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex(" ")
    return plain(value)


def sort_key(value: object) -> tuple:
    """Numbers by value, everything else as text."""
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return (0, float(value), "")
    return (1, 0, plain(value).lower())
