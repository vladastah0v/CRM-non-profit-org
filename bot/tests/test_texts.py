import uuid
from decimal import Decimal

import pytest

from bot import callbacks, texts

PK = "1f0e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


def test_callback_roundtrip_and_size():
    data = callbacks.assignment("confirm", PK)
    assert data == f"a:confirm:{uuid.UUID(PK).hex}" and len(data.encode()) <= 64
    parsed = callbacks.parse(data)
    assert parsed == callbacks.Callback("a", "confirm", PK)
    assert callbacks.parse(callbacks.shift("signup", PK)) == callbacks.Callback("s", "signup", PK)


@pytest.mark.parametrize(
    "data", [None, "", "a:confirm", "a:drop:" + "0" * 32, "x:confirm:" + "0" * 32, "a:confirm:zz"]
)
def test_callback_rejects_foreign_data(data):
    assert callbacks.parse(data) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("4", "4.00"), ("2,5", "2.50"), (" 3.75 ч", "3.75"), ("0,25", "0.25"), ("24", "24.00")],
)
def test_parse_hours(raw, expected):
    assert texts.parse_hours(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["", "abc", "0", "0.1", "25", "-3", "NaN", "Infinity", None])
def test_parse_hours_rejects(raw):
    assert texts.parse_hours(raw) is None


def test_format_period_and_hours():
    period = texts.format_period("2026-10-09T10:00:00+03:00", "2026-10-09T14:30:00+03:00")
    assert period == "пт, 09.10 с 10:00 до 14:30"
    night = texts.format_period("2026-10-09T22:00:00+03:00", "2026-10-10T02:00:00+03:00")
    assert night.endswith("(10.10)")
    assert texts.format_hours("4.00") == "4"
    assert texts.format_hours("3.50") == "3,5"
    assert texts.format_hours("10.00") == "10"


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("ABCD-EFGH", True),
        ("abcd efgh", True),
        ("ABCDEFGH", True),
        ("Мои смены", False),
        ("ABC-DEFGH", False),
        ("ABCD-EFGH-1", False),
    ],
)
def test_looks_like_code(text, ok):
    assert texts.looks_like_code(text) is ok


def visit(**fields):
    return {
        "id": PK,
        "kind": "visit",
        "title": "Визит к подопечному",
        "starts_at": "2026-10-09T10:00:00+03:00",
        "ends_at": "2026-10-09T12:00:00+03:00",
        "address": None,
        "description": "",
        "status": "assigned",
        "status_display": "Назначен",
        "hours": None,
        **fields,
    }


def test_visit_details_only_when_available():
    shown = texts.assignment_details(
        visit(visit={"name": "Пётр", "address": "ул. Ленина, 1", "phone": "+7", "comment": ""})
    )
    assert "Подопечный: Пётр" in shown and "Адрес: ул. Ленина, 1" in shown
    hidden = texts.assignment_details(visit(visit=None))
    assert "только на время" in hidden
