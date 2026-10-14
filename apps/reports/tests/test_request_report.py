"""ФТ-О-05: отчёт по заявкам — открытые, просроченные, среднее время выполнения."""

import datetime
import io
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.audit.models import AuditAction, AuditLog
from apps.beneficiaries.tests.conftest import to_in_work
from apps.reports import services
from apps.reports.services import Period
from apps.requests import services as request_services
from apps.requests.models import HelpRequest
from apps.requests.tests.conftest import assistance_data

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate()


def day(offset: int) -> datetime.date:
    return TODAY + datetime.timedelta(days=offset)


def noon(offset: int) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(day(offset), datetime.time(12)))


# Прошлый период: с 60 по 31 день назад
PERIOD = Period(day(-60), day(-31))


@pytest.fixture
def make_case(make_beneficiary, food, curator):
    """Заявка с заданной историей: поступила, (выполнена | отклонена) в заданные дни."""

    def factory(
        *,
        created: int,
        done: int | None = None,
        rejected: int | None = None,
        due: int | None = None,
        beneficiary=None,
    ) -> HelpRequest:
        beneficiary = beneficiary or to_in_work(make_beneficiary())
        help_request = request_services.create_request(
            beneficiary, {"assistance_type": food, "description": "Продукты", "urgency": "normal"}
        )
        if done is not None:
            request_services.change_status(help_request, "in_work")
            request_services.register_assistance(
                beneficiary, assistance_data(food, curator), help_request=help_request
            )
            request_services.change_status(help_request, "done")
            HelpRequest.objects.filter(pk=help_request.pk).update(
                status_changed_at=noon(done), completed_at=noon(done)
            )
        elif rejected is not None:
            request_services.change_status(help_request, "rejected", reason="Дубль")
            HelpRequest.objects.filter(pk=help_request.pk).update(status_changed_at=noon(rejected))
        HelpRequest.objects.filter(pk=help_request.pk).update(
            created_at=noon(created), due_date=day(due) if due is not None else None
        )
        return help_request

    return factory


@pytest.fixture
def history(make_case):
    return {
        "done_5_days": make_case(created=-50, done=-45),
        "done_2_days": make_case(created=-35, done=-33),
        "open_overdue": make_case(created=-40, due=-35),
        "open_not_yet_due": make_case(created=-40, due=-31),
        "closed_after_period": make_case(created=-100, done=-20),
        "rejected": make_case(created=-55, rejected=-50),
        "after_period": make_case(created=-10),
    }


def test_fto05_past_period(make_user, history):
    report = services.request_report(make_user("analyst"), PERIOD)
    assert report.received == 5  # без поступившей до и после периода
    assert report.done == 2
    assert report.rejected == 1 and report.cancelled == 0
    assert report.average_days == Decimal("3.5")
    assert report.median_days == Decimal("3.5")
    # Открыты на конец периода: две открытые до сих пор и выполненная уже после периода
    assert report.open_at_end == 3
    # Срок, наступивший в последний день периода, к его концу уже прошёл
    assert report.overdue_at_end == 2
    assert report.state_label == f"на конец {PERIOD.date_to:%d.%m.%Y}"
    (row,) = report.by_type
    assert (row.name, row.received, row.done, row.average_days) == (
        "Продуктовый набор",
        5,
        2,
        Decimal("3.5"),
    )
    assert dict(report.open_by_urgency)["Обычная"] == 3


def test_fto05_current_period_counts_now(make_user, history):
    report = services.request_report(make_user("admin"), Period(day(-30), TODAY))
    assert report.state_label == "сейчас"
    assert report.received == 1
    assert report.done == 1  # выполнена 20 дней назад
    # Сейчас открыты: две из прошлого периода и одна новая
    assert report.open_at_end == 3
    assert report.overdue_at_end == 2


def test_fto05_curator_sees_only_own(make_user, make_case, make_beneficiary, history):
    other = make_user("curator")
    make_case(created=-40, beneficiary=to_in_work(make_beneficiary(curator=other)))
    report = services.request_report(other, PERIOD)
    # Заявки подопечных другого куратора (history) в отчёт не попали
    assert report.scope == "мои подопечные"
    assert report.received == 1 and report.open_at_end == 1


def test_fto05_archived_not_counted(make_user, make_case):
    archived = make_case(created=-40)
    HelpRequest.objects.filter(pk=archived.pk).update(is_archived=True)
    assert services.request_report(make_user("admin"), PERIOD).received == 0


def test_fto05_page_and_xlsx_with_audit(verified_client, make_user, history):
    client = verified_client(make_user("analyst"))
    params = {"date_from": PERIOD.date_from.isoformat(), "date_to": PERIOD.date_to.isoformat()}
    page = client.get(reverse("reports:requests"), params)
    assert page.status_code == 200
    assert "Отчёт по заявкам" in page.content.decode()
    response = client.get(reverse("reports:requests"), params | {"format": "xlsx"})
    workbook = load_workbook(io.BytesIO(response.content))
    totals = {row[0]: row[1] for row in workbook["Итоги"].iter_rows(values_only=True) if row[0]}
    assert totals["Поступило заявок"] == 5
    assert totals["Из них просроченных"] == 2
    log = AuditLog.objects.get(action=AuditAction.EXPORT)
    assert log.object_type == "reports.requests" and log.extra["scope"] == "all"


@pytest.mark.parametrize(
    ("role", "allowed"),
    [("admin", True), ("analyst", True), ("curator", True), ("coordinator", False)],
)
def test_fto05_access(verified_client, make_user, role, allowed):
    status = verified_client(make_user(role)).get(reverse("reports:requests")).status_code
    assert (status == 200) == allowed
