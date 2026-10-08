"""ФТ-О-04: выгрузка списков в Excel с фильтрами и в пределах прав (матрица 2.2, п. 9.1)."""

import io

import pytest
from django.urls import reverse
from openpyxl import load_workbook

from apps.audit.models import AuditAction, AuditLog
from apps.beneficiaries.tests.conftest import to_in_work
from apps.requests import services as request_services
from apps.shifts import services as shift_services

pytestmark = pytest.mark.django_db


def rows_of(response) -> list[tuple]:
    ws = load_workbook(io.BytesIO(response.content)).active
    rows = list(ws.iter_rows(values_only=True))
    header_index = next(
        i for i, r in enumerate(rows) if r[0] in ("Фамилия", "Подопечный", "Название")
    )
    return rows[header_index:]


def export(client, kind, **params):
    return client.get(reverse("reports:export", args=[kind]), params)


def test_volunteers_export_full_and_filtered(
    verified_client, coordinator, make_active, make_volunteer
):
    active = make_active()
    make_volunteer(last_name="Анкетова")
    client = verified_client(coordinator)
    rows = rows_of(export(client, "volunteers", status="active", q="Петрова"))
    header, *data = rows
    assert "Дата рождения" in header and "Отработано часов" in header
    assert [r[0] for r in data] == [active.last_name]
    log = AuditLog.objects.get(action=AuditAction.EXPORT)
    assert log.object_type == "volunteers.list"
    assert log.extra == {"rows": 1, "filters": {"status": ["active"]}, "search": True}
    assert "Петрова" not in str(log.extra)  # текст поиска в журнал не попадает


def test_volunteers_export_limited_for_curator(verified_client, curator, make_active):
    make_active()
    header, row = rows_of(export(verified_client(curator), "volunteers"))
    assert "Дата рождения" not in header and "Email" not in header
    assert len(row) == len(header) == 8


def test_beneficiaries_export_respects_ownership(
    verified_client, make_user, curator, make_beneficiary
):
    mine = make_beneficiary()
    foreign = make_beneficiary(curator=make_user("curator"))
    header, *data = rows_of(export(verified_client(curator), "beneficiaries"))
    assert [r[0] for r in data] == [mine.last_name]  # чужого подопечного нет (п. 9.1)
    assert foreign.last_name not in str(data)
    assert "Дата рождения" in header


def test_beneficiaries_export_limited_for_coordinator(
    verified_client, coordinator, make_beneficiary
):
    make_beneficiary()
    header, _row = rows_of(export(verified_client(coordinator), "beneficiaries"))
    assert header == (
        "Фамилия",
        "Имя",
        "Отчество",
        "Телефон",
        "Адрес",
        "Категория",
        "Статус",
        "Куратор",
    )


def test_curator_coordinator_gets_only_limited_fields_of_foreign(
    verified_client, make_user, make_beneficiary
):
    both = make_user("curator", "coordinator")
    mine = make_beneficiary(curator=both)
    make_beneficiary(curator=make_user("curator"))
    header, *data = rows_of(export(verified_client(both), "beneficiaries"))
    birth = header.index("Дата рождения")
    by_name = {r[0]: r for r in data}
    assert by_name[mine.last_name][birth] is not None
    foreign_row = next(r for name, r in by_name.items() if name != mine.last_name)
    assert foreign_row[birth] in (None, "")


def test_requests_and_shifts_export(verified_client, curator, make_beneficiary, food, make_shift):
    beneficiary = to_in_work(make_beneficiary())
    request_services.create_request(
        beneficiary, {"assistance_type": food, "description": "Секретное описание"}
    )
    client = verified_client(curator)
    header, row = rows_of(export(client, "requests"))
    assert row[0] == beneficiary.full_name and "Секретное описание" not in str(row)
    make_shift()
    header, row = rows_of(export(client, "shifts", period="upcoming"))
    assert header[0] == "Название" and row[0] == "Раздача продуктов"


def test_export_access(verified_client, make_user, make_shift):
    analyst = verified_client(make_user("analyst"))
    for kind in ("volunteers", "beneficiaries", "requests", "shifts"):
        assert export(analyst, kind).status_code == 403
    assert export(analyst, "users").status_code == 404


def test_results_have_excel_button(verified_client, coordinator, make_shift, make_active):
    shift = make_shift()
    shift_services.assign_to_shift(shift, make_active())
    page = verified_client(coordinator).get(reverse("shifts:list"), {"period": "upcoming"})
    assert reverse("reports:export", args=["shifts"]) + "?period=upcoming" in page.content.decode()
