"""Выгрузки в Excel (XLSX): отчёты, списки, шаблоны и отчёты импорта (ФТ-О-04, п. 7.3).

Текст, начинающийся с «=», openpyxl записал бы как формулу: значение из карточки
(«=HYPERLINK(...)») выполнилось бы у сотрудника в Excel. Поэтому такие строки
принудительно записываются как текст.
"""

from __future__ import annotations

import datetime
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from django.http import HttpResponse
from django.utils import timezone
from openpyxl import Workbook
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
    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet in sheets:
        ws = workbook.create_sheet(sheet.title.translate(_SHEET_FORBIDDEN)[:31] or "Лист")
        for line in sheet.caption:
            ws.append([line])
            ws.cell(row=ws.max_row, column=1).font = Font(bold=ws.max_row == 1)
        if sheet.caption:
            ws.append([])
        ws.append(list(sheet.header))
        header_row = ws.max_row
        for col in range(1, len(sheet.header) + 1):
            cell = ws.cell(row=header_row, column=col)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        widths = [len(str(h)) for h in sheet.header]
        for row in sheet.rows:
            values = [cell_value(v) for v in row]
            ws.append(values)
            for col, value in enumerate(values, start=1):
                cell = ws.cell(row=ws.max_row, column=col)
                if isinstance(value, str) and value.startswith("="):
                    cell.value = value
                    cell.data_type = "s"  # текст, не формула
                elif isinstance(value, datetime.datetime):
                    cell.number_format = "DD.MM.YYYY HH:MM"
                elif isinstance(value, datetime.date):
                    cell.number_format = "DD.MM.YYYY"
                if col <= len(widths):
                    widths[col - 1] = max(widths[col - 1], min(len(str(value)), MAX_COLUMN_WIDTH))
        for col, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(col)].width = min(width + 2, MAX_COLUMN_WIDTH)
        ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


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
