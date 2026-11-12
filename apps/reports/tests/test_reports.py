"""Отчёты ФТ-О-01…03: сверка с ручным расчётом на известных данных, права, XLSX, аудит."""

import datetime
import io
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.audit.models import AuditAction, AuditLog
from apps.beneficiaries.tests.conftest import to_in_work
from apps.core.models import AssistanceType, BeneficiaryCategory
from apps.core.tests.markup import check_markup
from apps.core.xlsx import Sheet, build_workbook
from apps.reports import services
from apps.reports.services import WITHOUT_PROGRAM, Period
from apps.requests import services as request_services
from apps.requests.tests.conftest import assistance_data
from apps.shifts import services as shift_services
from apps.shifts.models import AssignmentStatus
from apps.shifts.tests.conftest import DAY, past_shift
from apps.volunteers import services as volunteer_services
from apps.volunteers.models import Volunteer, VolunteerStatusChange

TODAY = timezone.localdate
pytestmark = pytest.mark.django_db


def this_month() -> Period:
    return Period(TODAY() - datetime.timedelta(days=20), TODAY())


def read_xlsx(response) -> dict[str, list[tuple]]:
    workbook = load_workbook(io.BytesIO(response.content))
    return {ws.title: list(ws.iter_rows(values_only=True)) for ws in workbook.worksheets}


def test_previous_quarter():
    assert services.previous_quarter(datetime.date(2026, 10, 8)) == Period(
        datetime.date(2026, 7, 1), datetime.date(2026, 9, 30)
    )
    assert services.previous_quarter(datetime.date(2026, 2, 1)) == Period(
        datetime.date(2025, 10, 1), datetime.date(2025, 12, 31)
    )


# ---------------------------------------------------------------------------
# ФТ-О-01
# ---------------------------------------------------------------------------
@pytest.fixture
def program_data(make_beneficiary, make_user, curator, food, brefs):
    """Три подопечных, две программы: числа ниже посчитаны вручную."""
    program, other = brefs["program"], brefs["program"].__class__.objects.create(name="Другая")
    meds = AssistanceType.objects.create(name="Лекарства", unit="упак.")
    pensioners = BeneficiaryCategory.objects.get(name="Пожилые одинокие")
    second_curator = make_user("curator")
    a = to_in_work(make_beneficiary(category=pensioners))
    b = to_in_work(make_beneficiary())
    c = to_in_work(make_beneficiary(curator=second_curator))
    day = TODAY() - datetime.timedelta(days=3)
    old = TODAY() - datetime.timedelta(days=60)
    rows = [
        (a, food, Decimal(2), None, program, day),
        (a, food, Decimal(1), None, program, day),
        (a, meds, Decimal(3), Decimal("450.50"), program, day),
        (b, meds, None, Decimal("1000.00"), program, day),
        (c, food, Decimal(5), None, program, day),
        (c, food, Decimal(1), None, other, day),  # другая программа
        (a, food, Decimal(7), None, program, old),  # вне периода
        (b, food, Decimal(1), None, None, day),  # без программы
    ]
    for beneficiary, kind, quantity, amount, prog, when in rows:
        request_services.register_assistance(
            beneficiary,
            assistance_data(
                kind, curator, quantity=quantity, amount=amount, program=prog, provided_on=when
            ),
        )
    archived = request_services.register_assistance(
        a, assistance_data(food, curator, quantity=Decimal(9), program=program, provided_on=day)
    )
    request_services.archive_assistance(archived)  # ошибочная запись не считается
    return {"program": program, "second_curator": second_curator}


def test_fto01_program_report_matches_manual_count(make_user, program_data):
    admin = make_user("admin")
    report = services.program_report(admin, this_month(), program_data["program"])
    assert report.beneficiaries == 3
    assert report.facts == 5
    assert report.amount == Decimal("1450.50")
    by_type = {(r.name, r.unit): (r.facts, r.quantity, r.amount) for r in report.by_type}
    assert by_type == {
        ("Продуктовый набор", "шт."): (3, Decimal(8), Decimal(0)),
        ("Лекарства", "упак."): (2, Decimal(3), Decimal("1450.50")),
    }
    assert dict(report.by_category) == {"Пожилые одинокие": 1, "Без категории": 2}
    no_program = services.program_report(admin, this_month(), WITHOUT_PROGRAM)
    assert (no_program.facts, no_program.beneficiaries) == (1, 1)
    everything = services.program_report(admin, this_month(), None)
    assert (everything.facts, everything.beneficiaries) == (7, 3)


def test_fto01_curator_sees_only_own_beneficiaries(curator, program_data):
    report = services.program_report(curator, this_month(), program_data["program"])
    assert (report.beneficiaries, report.facts, report.scope) == (2, 4, "мои подопечные")
    assert report.volunteer_hours is None  # часы волонтёров — только в отчёте по всем


def test_fto01_volunteer_hours_by_program(make_user, make_shift, make_active, brefs):
    volunteer = make_active()
    shift = make_shift(program=brefs["program"])
    assignment = shift_services.assign_to_shift(shift, volunteer)
    past_shift(shift)
    assignment.shift = shift
    shift_services.change_assignment_status(assignment, AssignmentStatus.ATTENDED)
    volunteer_services.add_manual_hours(
        volunteer,
        worked_on=TODAY(),
        hours=Decimal("1.5"),
        comment="Склад",
        program=brefs["program"],
    )
    report = services.program_report(make_user("analyst"), this_month(), brefs["program"])
    assert (report.volunteer_hours, report.volunteers) == (Decimal("5.50"), 1)


def test_fto01_page_and_xlsx_with_audit(verified_client, make_user, program_data):
    client = verified_client(make_user("analyst"))
    period = this_month()
    params = {
        "date_from": period.date_from.isoformat(),
        "date_to": period.date_to.isoformat(),
        "program": str(program_data["program"].pk),
    }
    page = client.get(reverse("reports:program"), params)
    check_markup(page.content.decode())
    assert page.context["report"].facts == 5
    response = client.get(reverse("reports:program"), params | {"format": "xlsx"})
    assert response["Content-Type"].startswith("application/vnd.openxmlformats")
    assert response["Cache-Control"] == "no-store, private"
    sheets = read_xlsx(response)
    totals = {row[0]: row[1] for row in sheets["Итоги"] if row and row[0]}
    assert totals["Уникальных подопечных"] == 3 and totals["Фактов помощи"] == 5
    log = AuditLog.objects.get(action=AuditAction.EXPORT)
    assert log.object_type == "reports.program" and log.extra["scope"] == "all"


def test_fto01_default_period_is_previous_quarter(verified_client, curator):
    page = verified_client(curator).get(reverse("reports:program"))
    assert page.context["report"].period == services.previous_quarter()


# ---------------------------------------------------------------------------
# ФТ-О-02, ФТ-О-03
# ---------------------------------------------------------------------------
def test_fto02_volunteer_report(make_user, make_active, make_volunteer, make_shift, refs):
    period = this_month()
    before = timezone.now() - datetime.timedelta(days=40)
    veteran = make_active()
    # «Ветеран» стал активным до периода: сдвигаем его историю статусов в прошлое
    changes = VolunteerStatusChange.objects.filter(volunteer=veteran).order_by("created_at")
    for i, change in enumerate(changes):
        VolunteerStatusChange.objects.filter(pk=change.pk).update(
            created_at=before + datetime.timedelta(seconds=i)
        )
    Volunteer.objects.filter(pk=veteran.pk).update(created_at=before, start_date=before.date())
    newcomer = make_active()
    leaver = make_active()
    volunteer_services.change_status(leaver, "left", reason=refs["reason"])
    make_volunteer()  # анкета в периоде

    shift = make_shift()
    came = shift_services.assign_to_shift(shift, veteran)
    missed = shift_services.assign_to_shift(shift, newcomer)
    past_shift(shift)
    for assignment, status in ((came, "attended"), (missed, "no_show")):
        assignment.shift = shift
        shift_services.change_assignment_status(assignment, status)
    volunteer_services.add_manual_hours(
        newcomer, worked_on=TODAY(), hours=Decimal(2), comment="Звонки"
    )

    report = services.volunteer_report(make_user("coordinator"), period)
    assert report.active_at_start == 1  # только ветеран
    assert report.active_at_end == 2  # ветеран и новичок; выбывший — нет
    assert (report.new_applications, report.newly_active, report.left) == (3, 2, 1)
    assert (report.shift_hours, report.manual_hours, report.total_hours) == (
        Decimal("4.00"),
        Decimal("2.00"),
        Decimal("6.00"),
    )
    assert (report.attended, report.no_show, report.no_show_rate) == (1, 1, Decimal("50.0"))
    assert report.working_volunteers == 2 and report.shifts_held == 1
    rows = {r.name: (r.total, r.attended, r.no_show) for r in report.rows}
    assert rows[veteran.full_name] == (Decimal("4.00"), 1, 0)
    assert rows[newcomer.full_name] == (Decimal("2.00"), 0, 1)
    analyst_report = services.volunteer_report(make_user("analyst"), period)
    assert analyst_report.rows == [] and not analyst_report.with_names  # без ФИО


def test_fto03_funnel(make_user, make_active, make_volunteer, refs):
    make_active()
    make_active()
    stuck = make_volunteer()
    volunteer_services.change_status(stuck, "interview")
    dropped = make_volunteer()
    volunteer_services.change_status(dropped, "left", reason=refs["reason"])
    make_volunteer()
    report = services.funnel_report(make_user("analyst"), this_month())
    assert report.cohort == 5
    assert [(s.label, s.reached) for s in report.steps] == [
        ("Анкета", 5),
        ("Собеседование", 3),
        ("Обучение", 2),
        ("Активен", 2),
    ]
    assert report.steps[1].from_previous == Decimal("60.0")
    assert report.steps[3].from_start == Decimal("40.0")
    assert report.left_before_active == 1
    assert report.average_days_to_active == Decimal("0.0")
    assert dict(report.current)["Активен"] == 2


def test_volunteer_reports_pages_and_xlsx(verified_client, coordinator, make_active):
    make_active()
    client = verified_client(coordinator)
    for name in ("reports:volunteers", "reports:funnel"):
        page = client.get(reverse(name))
        check_markup(page.content.decode())
        response = client.get(reverse(name), {"format": "xlsx"})
        assert response.status_code == 200 and read_xlsx(response)
    assert AuditLog.objects.filter(action=AuditAction.EXPORT).count() == 2


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("admin", {"index", "program", "volunteers", "funnel"}),
        ("analyst", {"index", "program", "volunteers", "funnel"}),
        ("coordinator", {"index", "volunteers", "funnel"}),
        ("curator", {"index", "program"}),
    ],
)
def test_matrix_report_access(verified_client, make_user, role, allowed):
    client = verified_client(make_user(role))
    for name in ("index", "program", "volunteers", "funnel"):
        status = client.get(reverse(f"reports:{name}")).status_code
        assert (status == 200) == (name in allowed), (role, name, status)


def test_period_validation(verified_client, make_user):
    client = verified_client(make_user("analyst"))
    response = client.get(
        reverse("reports:funnel"), {"date_from": "2026-05-01", "date_to": "2026-04-01"}
    )
    assert response.context["report"] is None and response.context["form"].errors
    response = client.get(
        reverse("reports:funnel"),
        {"date_from": "2026-05-01", "date_to": "2026-04-01", "format": "xlsx"},
    )
    assert response["Content-Type"].startswith("text/html")  # без отчёта — без выгрузки


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------
def test_xlsx_text_is_never_a_formula():
    content = build_workbook(
        [Sheet("Лист/1", ["Имя", "Дата"], [('=HYPERLINK("http://x")', TODAY())])]
    )
    workbook = load_workbook(io.BytesIO(content))
    ws = workbook.active
    assert ws.title == "Лист 1"
    cell = ws.cell(row=2, column=1)
    assert cell.data_type == "s" and cell.value.startswith("=HYPERLINK")
    assert ws.cell(row=2, column=2).number_format == "DD.MM.YYYY"


def test_old_shift_days_not_in_period(make_user, make_shift, make_active):
    shift = make_shift(starts_in=-40 * DAY)
    assignment = shift_services.assign_to_shift(shift, make_active())
    shift_services.change_assignment_status(assignment, AssignmentStatus.ATTENDED)
    report = services.volunteer_report(make_user("admin"), this_month())
    assert report.shift_hours == 0 and report.shifts_held == 0


def test_xlsx_streams_rows_and_preserves_metadata():
    def rows():
        for i in range(2000):
            yield (f"=value{i}", TODAY())

    content = build_workbook([Sheet("Данные", ["Имя", "Дата"], rows(), caption=["=caption"])])
    workbook = load_workbook(io.BytesIO(content))
    sheet = workbook.active
    assert sheet.max_row == 2003
    assert sheet.freeze_panes == "A4"
    assert sheet["A1"].data_type == "s"
    assert sheet["A3"].font.bold
    assert sheet["A2003"].value == "=value1999"
    assert sheet["A2003"].data_type == "s"
    assert sheet["B2003"].number_format == "DD.MM.YYYY"
