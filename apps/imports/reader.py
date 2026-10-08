"""Чтение XLSX и CSV для импорта.

Защита от вредных файлов: XLSX — это zip, поэтому до разбора проверяются число и
распакованный размер частей (zip-бомба); XML разбирается через defusedxml (openpyxl
использует его сам, если он установлен). Лимиты — размер файла и число строк.
"""

from __future__ import annotations

import csv
import datetime
import io
import zipfile
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError

MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 50
MAX_UNPACKED = 50 * 1024 * 1024
MAX_ZIP_ENTRIES = 200

XLSX = "xlsx"
CSV = "csv"


@dataclass
class Table:
    header: list[str]
    rows: list[tuple[int, dict[str, Any]]]  # номер строки в файле, значения по заголовкам


def normalize_header(value: Any) -> str:
    """«Дата рождения*» → «дата рождения»: регистр, пробелы и звёздочка не важны."""
    return " ".join(str(value or "").replace("*", " ").split()).lower()


def detect_format(name: str, head: bytes) -> str:
    if head.startswith(b"PK\x03\x04"):
        return XLSX
    if name.lower().endswith(".csv"):
        return CSV
    raise ValidationError("Нужен файл Excel (.xlsx) или CSV.")


def _check_zip(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise ValidationError("Файл Excel повреждён.") from exc
    if len(entries) > MAX_ZIP_ENTRIES or sum(e.file_size for e in entries) > MAX_UNPACKED:
        raise ValidationError("Файл Excel слишком большой после распаковки.")


def _cell(value: Any) -> Any:
    if isinstance(value, datetime.datetime):
        return value.date() if value.time() == datetime.time.min else value
    if isinstance(value, float):
        # Excel хранит числа как float: 4.0 → «4», 2.5 → «2.5» — без хвостов округления
        return Decimal(repr(value)).normalize()
    if isinstance(value, str):
        return value.strip()
    return value


def _build(raw_rows: list[tuple[int, list[Any]]]) -> Table:
    if not raw_rows:
        raise ValidationError("Файл пустой.")
    _, header_cells = raw_rows[0]
    header = [normalize_header(h) for h in header_cells[:MAX_COLUMNS]]
    rows: list[tuple[int, dict[str, Any]]] = []
    for number, cells in raw_rows[1:]:
        values = {
            header[i]: _cell(value)
            for i, value in enumerate(cells[: len(header)])
            if header[i] and value not in (None, "")
        }
        if values:
            rows.append((number, values))
        if len(rows) > MAX_ROWS:
            raise ValidationError(f"Больше {MAX_ROWS} строк — разделите файл на части.")
    return Table(header=[h for h in header if h], rows=rows)


def read_xlsx(data: bytes) -> Table:
    from openpyxl import load_workbook

    _check_zip(data)
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl бросает разные исключения на испорченных файлах
        raise ValidationError("Не удалось прочитать файл Excel.") from exc
    try:
        sheet = workbook.worksheets[0]
        raw: list[tuple[int, list[Any]]] = []
        for number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            raw.append((number, list(row)))
            if number > MAX_ROWS + 1:
                raise ValidationError(f"Больше {MAX_ROWS} строк — разделите файл на части.")
    finally:
        workbook.close()
    return _build(raw)


def read_csv(data: bytes) -> Table:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover — cp1251 декодирует любые байты
        raise ValidationError("Не удалось определить кодировку CSV.")
    if "\x00" in text:
        raise ValidationError("Это не текстовый CSV-файл.")
    sample = text[:4096]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        dialect = type("ImportDialect", (csv.excel,), {"delimiter": delimiter})
    reader = csv.reader(io.StringIO(text), dialect)
    raw = []
    for number, row in enumerate(reader, start=1):
        raw.append((number, list(row)))
        if number > MAX_ROWS + 1:
            raise ValidationError(f"Больше {MAX_ROWS} строк — разделите файл на части.")
    return _build(raw)


def read_table(data: bytes, file_format: str) -> Table:
    return read_xlsx(data) if file_format == XLSX else read_csv(data)
