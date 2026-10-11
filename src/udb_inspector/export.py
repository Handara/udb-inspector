"""Write query results to CSV, Excel, JSON or tab-separated text."""

from __future__ import annotations

import csv
import datetime
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

FORMATS = {"csv": "CSV", "xlsx": "Excel", "json": "JSON", "clipboard": "Clipboard (tab-separated, pastes into Excel)"}
EXCEL_MAX_ROWS = 1_048_575  # 1,048,576 rows per sheet, minus the header


@dataclass(frozen=True)
class ExportReport:
    rows: int
    note: str | None = None  # e.g. rows Excel couldn't hold


Progress = Callable[[int], None]


def write(
    fmt: str, path: Path, columns: list[str], batches: Iterable[list[tuple]], progress: Progress | None = None
) -> ExportReport:
    writer = {"csv": _write_csv, "xlsx": _write_xlsx, "json": _write_json}[fmt]
    return writer(path, columns, batches, progress or (lambda count: None))


def tab_separated(columns: list[str], rows: Iterable[tuple]) -> str:
    """Rows as tab-separated text, the format spreadsheets paste as cells."""
    lines = ["\t".join(_flat(name) for name in columns)]
    lines += ["\t".join(_flat(_text(value)) for value in row) for row in rows]
    return "\n".join(lines) + "\n"


def _write_csv(path: Path, columns, batches, progress) -> ExportReport:
    count = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for batch in batches:
            writer.writerows([_text(value) for value in row] for row in batch)
            count += len(batch)
            progress(count)
    return ExportReport(count)


def _write_json(path: Path, columns, batches, progress) -> ExportReport:
    keys = _unique(columns)
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        fh.write("[")
        for batch in batches:
            for row in batch:
                fh.write(",\n  " if count else "\n  ")
                fh.write(json.dumps(dict(zip(keys, row)), default=_json_default, ensure_ascii=False))
                count += 1
            progress(count)
        fh.write("\n]\n" if count else "]\n")
    return ExportReport(count)


def _write_xlsx(path: Path, columns, batches, progress) -> ExportReport:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Results")
    header = []
    for name in columns:
        cell = WriteOnlyCell(sheet, value=_excel(name))
        cell.font = Font(bold=True)
        header.append(cell)
    sheet.append(header)
    count, left_out = 0, 0
    for batch in batches:
        for row in batch:
            if count >= EXCEL_MAX_ROWS:
                left_out += 1
                continue
            sheet.append([_excel(value) for value in row])
            count += 1
        progress(count)
    workbook.save(path)
    note = None
    if left_out:
        note = f"Excel holds {EXCEL_MAX_ROWS:,} rows per sheet, so {left_out:,} were left out. Use CSV for all of them."
    return ExportReport(count, note)


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)


def _excel(value: object) -> object:
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    if value is None or isinstance(value, (bool, int, float, Decimal, datetime.timedelta)):
        return value
    if isinstance(value, (datetime.datetime, datetime.time)) and value.tzinfo is not None:
        return value.isoformat()  # Excel has no time zones; keep the offset as text
    if isinstance(value, (datetime.date, datetime.time)):
        return value
    return ILLEGAL_CHARACTERS_RE.sub("", _text(value))


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


def _flat(text: str) -> str:
    return text.replace("\t", " ").replace("\r\n", " ").replace("\n", " ")


def _unique(columns: list[str]) -> list[str]:
    """JSON keys must differ: id, id → id, id_2."""
    seen: dict[str, int] = {}
    keys = []
    for name in columns:
        seen[name] = seen.get(name, 0) + 1
        keys.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return keys
