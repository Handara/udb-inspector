"""Turn rows pasted from a spreadsheet into values for a table, row by row."""

from __future__ import annotations

import csv
import datetime
import io
import json
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import types as sqltypes

OPERATIONS = {
    "insert": "Insert new rows",
    "update": "Update rows, matched by key",
    "upsert": "Upsert: update by key, insert if missing",
    "delete": "Delete rows, matched by key",
}
TRUE = {"true", "t", "yes", "y", "1"}
FALSE = {"false", "f", "no", "n", "0"}
THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: sqltypes.TypeEngine
    type_name: str
    nullable: bool
    has_default: bool  # an insert may leave it out
    primary_key: bool
    generated: bool  # numbered by the database (serial, identity, SQLite's rowid)


@dataclass(frozen=True)
class Pasted:
    headers: list[str]
    rows: list[list[str]]


@dataclass(frozen=True)
class PlannedRow:
    line: int  # the spreadsheet row; the header is row 1
    cells: list[str]
    values: dict[str, object] | None  # None when the row has a problem
    problem: str | None = None


@dataclass
class Plan:
    mapping: dict[int, str]  # pasted column -> table column
    ignored: list[str]  # pasted headers that match no column
    problems: list[str] = field(default_factory=list)  # stop the whole import
    rows: list[PlannedRow] = field(default_factory=list)

    @property
    def ready(self) -> list[PlannedRow]:
        return [row for row in self.rows if row.values is not None]

    @property
    def rejected(self) -> list[PlannedRow]:
        return [row for row in self.rows if row.values is None]


def parse_pasted(text: str) -> Pasted:
    """Cells copied from Excel are tab-separated; CSV with , or ; works too."""
    text = text.strip("\r\n")
    if not text.strip():
        return Pasted([], [])
    first = text.split("\n", 1)[0]
    if "\t" in first:
        delimiter = "\t"
    else:
        delimiter = max((";", ",", "|"), key=first.count) if any(d in first for d in ";,|") else "\t"
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    headers = [cell.strip() for cell in rows[0]]
    body = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    return Pasted(headers, body)


def _normal(name: str) -> str:
    return re.sub(r"[\s_\-]+", "", name).lower()


def match_columns(headers: list[str], columns: list[ColumnInfo]) -> tuple[dict[int, str], list[str]]:
    """Headers to columns, ignoring case, spaces, _ and -."""
    by_name = {_normal(column.name): column.name for column in columns}
    mapping, ignored = {}, []
    for index, header in enumerate(headers):
        column = by_name.get(_normal(header))
        if column is None or column in mapping.values():
            ignored.append(header or f"column {index + 1}")
        else:
            mapping[index] = column
    return mapping, ignored


def template_columns(columns: list[ColumnInfo], operation: str, key: list[str]) -> list[ColumnInfo]:
    """The columns a template for `operation` should have, key first."""
    keyed = [c for c in columns if c.name in key] + [c for c in columns if c.name not in key]
    if operation == "insert":
        return [c for c in columns if not c.generated]
    if operation == "delete":
        return [c for c in columns if c.name in key]
    return keyed


def plan(pasted: Pasted, columns: list[ColumnInfo], operation: str, key: list[str]) -> Plan:
    mapping, ignored = match_columns(pasted.headers, columns)
    result = Plan(mapping, ignored)
    if not pasted.headers:
        return result
    if not mapping:
        result.problems.append("None of the pasted headers is a column of this table. The first pasted row must be the column names.")
        return result
    mapped = set(mapping.values())
    if operation != "insert":
        missing = [k for k in key if k not in mapped]
        if missing:
            result.problems.append(f"{operation.capitalize()} matches rows by {', '.join(key)}; paste a column for {', '.join(missing)}.")
            return result
        if operation == "update" and not mapped - set(key):
            result.problems.append("Update needs at least one column to change besides the key.")
            return result
    if operation == "delete":  # only the key matters
        mapping = {index: column for index, column in mapping.items() if column in key}
        result.mapping = mapping

    by_name = {column.name: column for column in columns}
    for number, cells in enumerate(pasted.rows, start=2):
        values, problems = {}, []
        for index, name in mapping.items():
            column = by_name[name]
            raw = cells[index].strip() if index < len(cells) else ""
            if raw in ("", "NULL"):
                if name in key and operation != "insert":
                    problems.append(f"{name} is empty")
                elif operation == "insert" and column.has_default:
                    continue  # let the database fill it in
                elif not column.nullable:
                    problems.append(f"{name} is required")
                else:
                    values[name] = None
                continue
            try:
                values[name] = convert(raw, column)
            except ValueError as exc:
                problems.append(f"{name}: {exc}")
        if operation == "insert":
            required = [c.name for c in columns if not c.nullable and not c.has_default and c.name not in mapped]
            if required:
                problems.append(f"{', '.join(required)} {'is' if len(required) == 1 else 'are'} required")
        if problems:
            result.rows.append(PlannedRow(number, cells, None, "; ".join(problems)))
        else:
            result.rows.append(PlannedRow(number, cells, values))
    return result


def convert(raw: str, column: ColumnInfo) -> object:
    """A pasted cell as the Python value for the column's type."""
    kind = column.type
    if isinstance(kind, sqltypes.Boolean):
        lowered = raw.lower()
        if lowered in TRUE:
            return True
        if lowered in FALSE:
            return False
        raise ValueError(f"{raw!r} isn't true or false")
    if isinstance(kind, sqltypes.Integer):
        number = raw.replace(",", "") if THOUSANDS.match(raw) else raw
        try:
            return int(number)
        except ValueError:
            raise ValueError(f"{raw!r} isn't a whole number") from None
    if isinstance(kind, (sqltypes.Float, sqltypes.Numeric)):
        number = raw.replace(",", "") if THOUSANDS.match(raw) else raw
        try:
            value = Decimal(number)
        except InvalidOperation:
            raise ValueError(f"{raw!r} isn't a number") from None
        return float(value) if isinstance(kind, sqltypes.Float) else value
    if isinstance(kind, sqltypes.DateTime):
        return _datetime(raw)
    if isinstance(kind, sqltypes.Date):
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError:
            moment = _datetime(raw)
            if moment.time() != datetime.time():
                raise ValueError(f"{raw!r} has a time; this column holds dates") from None
            return moment.date()
    if isinstance(kind, sqltypes.Time):
        try:
            return datetime.time.fromisoformat(raw)
        except ValueError:
            raise ValueError(f"{raw!r} isn't a time; write it as HH:MM or HH:MM:SS") from None
    if isinstance(kind, sqltypes.JSON):
        try:
            return json.loads(raw)
        except ValueError:
            raise ValueError("isn't valid JSON") from None
    if isinstance(kind, sqltypes.LargeBinary):
        try:
            return bytes.fromhex(raw)
        except ValueError:
            raise ValueError("binary columns take hexadecimal, like 0a1b2c") from None
    if isinstance(kind, sqltypes.Uuid):
        try:
            value = uuid.UUID(raw)
        except ValueError:
            raise ValueError(f"{raw!r} isn't a UUID") from None
        return value if getattr(kind, "as_uuid", True) else str(value)
    return raw


def _datetime(raw: str) -> datetime.datetime:
    text = raw.replace("/", "-", 2) if re.match(r"^\d{4}/\d{1,2}/\d{1,2}", raw) else raw
    # Excel writes 9:05 rather than 09:05.
    text = re.sub(r"(?<=[ T])(\d)(?=:\d{2})", r"0\1", text)
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{raw!r} isn't a date; write dates as YYYY-MM-DD (format the column as text in Excel)") from None


def template_tsv(columns: list[ColumnInfo]) -> str:
    """The header row as tab-separated text, to paste into a spreadsheet."""
    return "\t".join(column.name for column in columns) + "\n"


def describe_column(column: ColumnInfo) -> str:
    """The note on a template's header cell."""
    facts = [column.type_name]
    if column.primary_key:
        facts.append("primary key")
    if column.generated:
        facts.append("numbered by the database")
    elif not column.nullable and not column.has_default:
        facts.append("required")
    elif column.has_default:
        facts.append("has a default when left empty")
    else:
        facts.append("optional")
    if isinstance(column.type, (sqltypes.Date, sqltypes.DateTime)):
        facts.append("write as YYYY-MM-DD" + (" HH:MM" if isinstance(column.type, sqltypes.DateTime) else ""))
    elif isinstance(column.type, sqltypes.Boolean):
        facts.append("true or false")
    return ", ".join(facts)


def save_template(path, columns: list[ColumnInfo]) -> None:
    """An .xlsx whose header cells carry notes on each column; dates stay text so Excel won't reformat them."""
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Font

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Import"
    for index, column in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=index, value=column.name)
        cell.font = Font(bold=True)
        cell.comment = Comment(describe_column(column), "udbi", width=260, height=90)
        sheet.column_dimensions[cell.column_letter].width = max(12, len(column.name) + 4)
        if isinstance(column.type, (sqltypes.Date, sqltypes.DateTime, sqltypes.Time)):
            for row in range(2, 1002):
                sheet.cell(row=row, column=index).number_format = "@"
    sheet.freeze_panes = "A2"
    workbook.save(path)
