"""Экраны смен: часы, архив, изменение визита, ошибки подбора и явки."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.tests.markup import check_markup
from apps.shifts import services
from apps.shifts.models import Assignment, AssignmentStatus

from .conftest import DAY, HOUR, NOW, past_shift

pytestmark = pytest.mark.django_db


@pytest.fixture
def coordinator_client(verified_client, coordinator):
    return verified_client(coordinator)


def test_assignment_hours_page(coordinator_client, make_shift, make_active):
    shift = make_shift()
    assignment = services.assign_to_shift(shift, make_active())
    url = reverse("shifts:assignment_hours", args=[assignment.pk])
    assert coordinator_client.get(url).status_code == 302  # ещё не «Пришёл»
    past_shift(shift)
    assignment.shift = shift
    services.change_assignment_status(assignment, AssignmentStatus.ATTENDED)
    check_markup(coordinator_client.get(url).content.decode())
    bad = coordinator_client.post(url, {"hours": "30"})
    assert bad.status_code == 200 and bad.context["form"].errors
    coordinator_client.post(url, {"hours": "2.25"})
    assignment.refresh_from_db()
    assert str(assignment.hours) == "2.25"


def test_assignment_status_errors(coordinator_client, make_shift, make_active):
    shift = make_shift()
    assignment = services.assign_to_shift(shift, make_active())
    url = reverse("shifts:assignment_status", args=[assignment.pk])
    response = coordinator_client.post(url, {"to_status": "cancelled", "hours": "3"})
    assert response.status_code == 200 and "hours" in response.context["form"].errors
    services.cancel_shift(shift, reason="Отмена")
    assert coordinator_client.get(url).status_code == 302  # смена отменена — переходов нет


def test_shift_archive(coordinator_client, make_shift, make_active):
    shift = make_shift()
    assignment = services.assign_to_shift(shift, make_active())
    response = coordinator_client.post(reverse("shifts:archive", args=[shift.pk]))
    assert response.status_code == 302
    shift.refresh_from_db()
    assignment.refresh_from_db()
    assert shift.is_archived and assignment.is_archived
    assert coordinator_client.get(reverse("shifts:pick", args=[shift.pk])).status_code == 302


def test_pick_reports_errors(coordinator_client, make_shift, make_active, make_volunteer):
    shift = make_shift()
    url = reverse("shifts:pick", args=[shift.pk])
    assert coordinator_client.post(url, {}).status_code == 302  # никого не отметили
    busy = make_active()
    services.assign_to_shift(make_shift(starts_in=DAY + HOUR), busy)
    not_active = make_volunteer()
    response = coordinator_client.post(url, {"volunteer": [busy.pk, not_active.pk]}, follow=True)
    text = response.content.decode()
    assert "уже есть назначение" in text and "«Активен»" in text
    assert not Assignment.objects.filter(shift=shift).exists()


def test_cancel_page_requires_reason(coordinator_client, make_shift):
    shift = make_shift()
    url = reverse("shifts:cancel", args=[shift.pk])
    check_markup(coordinator_client.get(url).content.decode())
    response = coordinator_client.post(url, {"reason": ""})
    assert response.status_code == 200
    shift.refresh_from_db()
    assert not shift.is_cancelled


def test_attendance_reports_rule_errors(coordinator_client, make_shift, make_active):
    shift = make_shift()
    assignment = services.assign_to_shift(shift, make_active())
    past_shift(shift)
    url = reverse("shifts:attendance", args=[shift.pk])
    response = coordinator_client.post(
        url, {f"mark_{assignment.pk}": "yes", f"hours_{assignment.pk}": "abc"}
    )
    assert response.status_code == 200  # ошибка формы — страница с ошибкой
    assignment.refresh_from_db()
    assert assignment.status == AssignmentStatus.ASSIGNED


def test_visit_edit(coordinator_client, in_work_beneficiary, make_active):
    starts = (NOW() + 2 * DAY).replace(minute=0, second=0, microsecond=0)
    (visit,) = services.create_visits(
        in_work_beneficiary, make_active(), starts_at=starts, ends_at=starts + HOUR
    )
    url = reverse("shifts:visit_edit", args=[visit.pk])
    page = coordinator_client.get(url)
    check_markup(page.content.decode())
    day = timezone.localtime(starts).date().isoformat()
    bad = coordinator_client.post(url, {"date": day, "time_from": "15:00", "time_to": "14:00"})
    assert "time_to" in bad.context["form"].errors
    coordinator_client.post(
        url, {"date": day, "time_from": "15:00", "time_to": "17:30", "task_comment": "Аптека"}
    )
    visit.refresh_from_db()
    assert timezone.localtime(visit.visit_starts_at).hour == 15
    assert visit.task_comment == "Аптека"
    services.change_assignment_status(visit, AssignmentStatus.DECLINED)
    assert coordinator_client.get(url).status_code == 302  # уже не предстоящий


def test_visit_detail_status_link_for_coordinator(
    coordinator_client, in_work_beneficiary, make_active
):
    starts = (NOW() + DAY).replace(minute=0, second=0, microsecond=0)
    (visit,) = services.create_visits(
        in_work_beneficiary, make_active(), starts_at=starts, ends_at=starts + HOUR
    )
    page = coordinator_client.get(reverse("shifts:visit_detail", args=[visit.pk]))
    assert page.context["transitions"] and page.context["can_open_card"]
    status_url = reverse("shifts:assignment_status", args=[visit.pk])
    coordinator_client.post(status_url, {"to_status": "confirmed"})
    visit.refresh_from_db()
    assert visit.status == AssignmentStatus.CONFIRMED
