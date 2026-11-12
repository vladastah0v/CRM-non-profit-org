"""Выгрузки в Excel (XLSX): отчёты, списки, шаблоны и отчёты импорта (ФТ-О-04, п. 7.3).

Текст, начинающийся с «=», openpyxl записал бы как формулу: значение из карточки
(«=HYPERLINK(...)») выполнилось бы у сотрудника в Excel. Поэтому такие строки
принудительно записываются как текст.
"""

from __future__ import annotations

import datetime
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from django.http import HttpResponse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_COLUMN_WIDTH = 60
# Excel не допускает в названии листа : \ / ? * [ ] и длину больше 31
_SHEET_FORBIDDEN = str.maketrans(dict.fromkeys(":\\/?*[]", " "))


@dataclass
class Sheet:
    title: str
    header: Sequence[str]
    rows: Iterable[Sequence[Any]]
    # Строки над таблицей: название отчёта, период, фильтры
    caption: Sequence[str] = field(default_factory=list)


def cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, datetime.datetime):
        # Excel не хранит часовой пояс: время — в поясе фонда
        return timezone.localtime(value).replace(tzinfo=None) if timezone.is_aware(value) else value
    if isinstance(value, datetime.date | int | float | Decimal):
        return value
    return str(value)


def build_workbook(sheets: Sequence[Sheet]) -> bytes:
    workbook = Workbook(write_only=True)
    for sheet in sheets:
        ws = workbook.create_sheet(sheet.title.translate(_SHEET_FORBIDDEN)[:31] or "Лист")
        header_row = len(sheet.caption) + (1 if sheet.caption else 0) + 1
        ws.freeze_panes = f"A{header_row + 1}"
        for col, label in enumerate(sheet.header, start=1):
            ws.column_dimensions[get_column_letter(col)].width = min(
                max(len(str(label)) + 2, 24), MAX_COLUMN_WIDTH
            )
        for index, line in enumerate(sheet.caption):
            cell = WriteOnlyCell(ws, value=str(line))
            cell.data_type = "s"
            cell.font = Font(bold=index == 0)
            ws.append([cell])
        if sheet.caption:
            ws.append([])
        header = []
        for label in sheet.header:
            cell = WriteOnlyCell(ws, value=str(label))
            cell.data_type = "s"
            cell.font = Font(bold=True)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            header.append(cell)
        ws.append(header)
        for row in sheet.rows:
            cells = []
            for raw in row:
                value = cell_value(raw)
                cell = WriteOnlyCell(ws, value=value)
                if isinstance(value, str):
                    cell.data_type = "s"
                elif isinstance(value, datetime.datetime):
                    cell.number_format = "DD.MM.YYYY HH:MM"
                elif isinstance(value, datetime.date):
                    cell.number_format = "DD.MM.YYYY"
                cells.append(cell)
            ws.append(cells)
    # Ячейки записываются на диск по мере обхода строк; в памяти остаётся готовый XLSX.
    with tempfile.TemporaryFile() as buffer:
        workbook.save(buffer)
        buffer.seek(0)
        return buffer.read()


def xlsx_response(content: bytes, filename: str) -> HttpResponse:
    """Файл на скачивание. Имя — с датой; кириллица — через filename* (RFC 6266)."""
    stamp = timezone.localdate().strftime("%Y-%m-%d")
    full_name = f"{filename}-{stamp}.xlsx"
    response = HttpResponse(content, content_type=CONTENT_TYPE)
    response["Content-Disposition"] = (
        f"attachment; filename=\"export-{stamp}.xlsx\"; filename*=UTF-8''{quote(full_name)}"
    )
    # В выгрузках персональные данные: не кэшировать ни в браузере, ни на прокси
    response["Cache-Control"] = "no-store, private"
    return response
