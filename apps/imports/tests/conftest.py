import io
from collections.abc import Sequence
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook

from apps.requests.tests.conftest import food  # noqa: F401
from apps.shifts.tests.conftest import (  # noqa: F401 — фикстуры модулей
    brefs,
    coordinator,
    curator,
    make_active,
    make_beneficiary,
    make_volunteer,
    refs,
)


def xlsx(header: Sequence[str], rows: Sequence[Sequence[Any]], name: str = "data.xlsx"):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(header))
    for row in rows:
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return SimpleUploadedFile(name, buffer.getvalue())


def csv_file(text: str, encoding: str = "cp1251", name: str = "data.csv"):
    return SimpleUploadedFile(name, text.encode(encoding))
