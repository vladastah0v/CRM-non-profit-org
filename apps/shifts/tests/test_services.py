"""Сервисы смен и назначений: ФТ-С-01…05, п. 4.3 (статусы назначения), п. 4.4."""

import datetime
from decimal import Decimal
from unittest import mock

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.beneficiaries import services as beneficiary_services
from apps.notifications.models import Notification, NotificationKind
from apps.shifts import services
from apps.shifts.models import Assignment, AssignmentStatus, AssignmentStatusChange, Shift
from apps.volunteers import services as volunteer_services

from .conftest import DAY, HOUR, NOW, past_shift

S = AssignmentStatus


# ---------------------------------------------------------------------------
# ФТ-С-01: смена
# ---------------------------------------------------------------------------
def test_fts01_create_shift_with_skills(make_shift, refs):
    shift = make_shift(skills=[refs["skill_drive"]], program=refs["program"])
    assert shift.status == "planned"
    assert list(shift.skills.all()) == [refs["skill_drive"]]
    assert shift.default_hours == Decimal("4.00")


def test_fts01_shift_end_must_be_after_start(make_shift):
    with pytest.raises(ValidationError) as exc:
        make_shift(hours=0)
    assert "ends_at" in exc.value.message_dict


def test_p44_db_constraint_end_after_start(make_shift):
    shift = make_shift()
    with pytest.raises(IntegrityError), transaction.atomic():
        Shift.objects.filter(pk=shift.pk).update(ends_at=shift.starts_at)


def test_update_shift_time_resets_reminders_and_notifies(
    make_shift, make_active, django_capture_on_commit_callbacks
):
    shift = make_shift(starts_in=3 * DAY)
    volunteer = make_active()
    type(volunteer).objects.filter(pk=volunteer.pk).update(telegram_id=111)
    volunteer.refresh_from_db()
    assignment = services.assign_to_shift(shift, volunteer)
    Assignment.objects.filter(pk=assignment.pk).update(reminded_day_at=NOW())
    with (
        mock.patch("apps.shifts.tasks.notify_volunteer.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        services.update_shift(
            shift, {"starts_at": shift.starts_at + HOUR, "ends_at": shift.ends_at + HOUR}
        )
    assignment.refresh_from_db()
    assert assignment.reminded_day_at is None
    delay.assert_called_once_with(str(assignment.pk), "changed", 1)


def test_cancel_shift_cancels_pending_assignments(make_shift, make_active):
    shift = make_shift()
    a1 = services.assign_to_shift(shift, make_active())
    a2 = services.assign_to_shift(shift, make_active())
    services.change_assignment_status(a2, S.DECLINED)
    with pytest.raises(ValidationError):
        services.cancel_shift(shift, reason="  ")
    services.cancel_shift(shift, reason="Нет продуктов")
    a1.refresh_from_db()
    a2.refresh_from_db()
    assert shift.status == "cancelled"
    assert a1.status == S.CANCELLED
    assert a2.status == S.DECLINED  # отказ остаётся отказом
    with pytest.raises(ValidationError):
        services.update_shift(shift, {"title": "Новая"})


def test_archive_shift_archives_assignments_and_hours(make_shift, make_active):
    volunteer = make_active()
    shift = make_shift(starts_in=-DAY)  # прошедшая смена: координатор вносит явку задним числом
    assignment = services.assign_to_shift(shift, volunteer)
    services.change_assignment_status(assignment, S.ATTENDED)
    assert volunteer_services.total_hours(volunteer) == Decimal("4.00")
    services.archive_shift(shift)
    assert volunteer_services.total_hours(volunteer) == 0


# ---------------------------------------------------------------------------
# ФТ-С-02: назначение координатором и запись через бота
# ---------------------------------------------------------------------------
def test_fts02_only_active_volunteers(make_shift, make_volunteer):
    with pytest.raises(ValidationError) as exc:
        services.assign_to_shift(make_shift(), make_volunteer())
    assert "volunteer" in exc.value.message_dict


def test_fts02_no_double_assignment(make_shift, make_active):
    shift, volunteer = make_shift(), make_active()
    services.assign_to_shift(shift, volunteer)
    with pytest.raises(ValidationError, match="уже записан"):
        services.assign_to_shift(shift, volunteer)


def test_fts02_overlap_with_other_assignment(make_shift, make_active):
    volunteer = make_active()
    services.assign_to_shift(make_shift(), volunteer)
    overlapping = make_shift(starts_in=DAY + 2 * HOUR)
    with pytest.raises(ValidationError, match="уже есть назначение"):
        services.assign_to_shift(overlapping, volunteer)
    # Смена сразу после — без пересечения
    services.assign_to_shift(make_shift(starts_in=DAY + 4 * HOUR), volunteer)


def test_fts02_reassign_after_decline_reuses_row(make_shift, make_active):
    shift, volunteer = make_shift(), make_active()
    first = services.assign_to_shift(shift, volunteer)
    services.change_assignment_status(first, S.DECLINED)
    second = services.assign_to_shift(shift, volunteer)
    assert second.pk == first.pk
    assert second.status == S.ASSIGNED
    assert list(
        AssignmentStatusChange.objects.filter(assignment=first)
        .order_by("created_at")
        .values_list("to_status", flat=True)
    ) == [S.ASSIGNED, S.DECLINED, S.ASSIGNED]


def test_fts02_bot_signup_rules(make_shift, make_active, refs):
    volunteer = make_active(skills=[refs["skill_drive"]])
    closed = make_shift(allow_signup=False)
    with pytest.raises(ValidationError, match="координатор"):
        services.assign_to_shift(closed, volunteer, via_bot=True)
    needs_med = make_shift(starts_in=2 * DAY, skills=[refs["skill_med"]])
    with pytest.raises(ValidationError, match="навыки"):
        services.assign_to_shift(needs_med, volunteer, via_bot=True)
    started = past_shift(make_shift(starts_in=3 * DAY), started_ago=HOUR)
    with pytest.raises(ValidationError, match="началась"):
        services.assign_to_shift(started, volunteer, via_bot=True)
    full = make_shift(starts_in=4 * DAY, volunteers_needed=1)
    services.assign_to_shift(full, make_active())
    with pytest.raises(ValidationError, match="мест"):
        services.assign_to_shift(full, volunteer, via_bot=True)
    # Координатор может назначить сверх нужного числа
    services.assign_to_shift(full, volunteer)
    ok = make_shift(starts_in=5 * DAY, skills=[refs["skill_drive"]])
    assignment = services.assign_to_shift(ok, volunteer, via_bot=True)
    assert assignment.status == S.ASSIGNED
    assert assignment.status_changes.get().via_bot is True


def test_assignment_within_day_counts_as_reminded(make_shift, make_active):
    soon = services.assign_to_shift(make_shift(starts_in=3 * HOUR), make_active())
    later = services.assign_to_shift(make_shift(starts_in=3 * DAY), make_active())
    assert soon.reminded_day_at is not None and soon.reminded_soon_at is None
    assert later.reminded_day_at is None


# ---------------------------------------------------------------------------
# ФТ-С-04, п. 4.3: подтверждение, явка, часы
# ---------------------------------------------------------------------------
def test_p43_confirm_and_decline_only_before_start(make_shift, make_active):
    shift = make_shift()
    assignment = services.assign_to_shift(shift, make_active())
    services.change_assignment_status(assignment, S.CONFIRMED)
    past_shift(shift, started_ago=HOUR)
    assignment.shift = shift
    with pytest.raises(ValidationError, match="началась"):
        services.change_assignment_status(assignment, S.DECLINED)


def test_fts04_attendance_only_after_start(make_shift, make_active):
    assignment = services.assign_to_shift(make_shift(), make_active())
    with pytest.raises(ValidationError, match="после начала"):
        services.change_assignment_status(assignment, S.ATTENDED)


def test_fts04_attended_gets_default_hours_and_counts(make_shift, make_active):
    volunteer = make_active()
    shift = make_shift()
    assignment = services.assign_to_shift(shift, volunteer)
    past_shift(shift)
    assignment.shift = shift
    services.change_assignment_status(assignment, S.ATTENDED)
    assert assignment.hours == Decimal("4.00")
    assert volunteer_services.total_hours(volunteer) == Decimal("4.00")
    services.update_hours(assignment, Decimal("3.5"))
    assert volunteer_services.shift_hours_total(volunteer) == Decimal("3.50")
    # Исправление ошибочной отметки: часы снимаются
    services.change_assignment_status(assignment, S.NO_SHOW)
    assert assignment.hours is None
    assert volunteer_services.total_hours(volunteer) == 0


def test_p43_hours_only_for_attended(make_shift, make_active):
    shift = make_shift()
    assignment = services.assign_to_shift(shift, make_active())
    past_shift(shift)
    assignment.shift = shift
    with pytest.raises(ValidationError) as exc:
        services.change_assignment_status(assignment, S.NO_SHOW, hours=Decimal(2))
    assert "hours" in exc.value.message_dict
    with pytest.raises(ValidationError):
        services.change_assignment_status(assignment, S.ATTENDED, hours=Decimal(25))
    with pytest.raises(IntegrityError), transaction.atomic():
        Assignment.objects.filter(pk=assignment.pk).update(hours=Decimal(2))


def test_p43_volunteer_cannot_override_coordinator_mark(make_shift, make_active):
    shift = make_shift()
    assignment = services.assign_to_shift(shift, make_active())
    past_shift(shift)
    assignment.shift = shift
    services.change_assignment_status(assignment, S.NO_SHOW)
    with pytest.raises(ValidationError, match="координатором"):
        services.change_assignment_status(assignment, S.ATTENDED, via_bot=True)


def test_record_attendance_sheet(make_shift, make_active):
    shift = make_shift()
    a = services.assign_to_shift(shift, make_active())
    past_shift(shift)
    a.shift = shift
    services.record_attendance(a, attended=True, hours=Decimal("2.5"))
    assert (a.status, a.hours) == (S.ATTENDED, Decimal("2.50"))
    services.record_attendance(a, attended=True, hours=Decimal(3))
    assert a.hours == Decimal("3.00")
    services.record_attendance(a, attended=False)
    assert a.status == S.NO_SHOW
    services.record_attendance(a, attended=False)  # повтор — без ошибки


def test_allowed_transitions_by_time(make_shift, make_active):
    shift = make_shift()
    assignment = services.assign_to_shift(shift, make_active())
    assert services.allowed_transitions(assignment) == [S.CONFIRMED, S.DECLINED, S.CANCELLED]
    past_shift(shift)
    assignment.shift = shift
    assert services.allowed_transitions(assignment) == [S.ATTENDED, S.NO_SHOW, S.CANCELLED]


def test_decline_via_bot_notifies_planner(make_shift, make_active, coordinator):
    from apps.core.context import acting_as

    with acting_as(coordinator):
        shift = make_shift()
    assignment = services.assign_to_shift(shift, make_active())
    services.change_assignment_status(assignment, S.DECLINED, via_bot=True)
    notification = Notification.objects.get(kind=NotificationKind.ASSIGNMENT_DECLINED)
    assert notification.recipient == coordinator
    assert "Петрова" not in notification.title + notification.body  # без ФИО


# ---------------------------------------------------------------------------
# ФТ-С-05: визиты к подопечным
# ---------------------------------------------------------------------------
def visit_times(days: int = 1) -> tuple[datetime.datetime, datetime.datetime]:
    starts = (NOW() + datetime.timedelta(days=days)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    return starts, starts + 2 * HOUR


def test_fts05_visit_requires_case_in_work(make_beneficiary, make_active):
    starts, ends = visit_times()
    with pytest.raises(ValidationError, match="В работе"):
        services.create_visits(make_beneficiary(), make_active(), starts_at=starts, ends_at=ends)


def test_fts05_regular_visits(in_work_beneficiary, make_active):
    starts, ends = visit_times()
    volunteer = make_active()
    visits = services.create_visits(
        in_work_beneficiary,
        volunteer,
        starts_at=starts,
        ends_at=ends,
        task_comment="  Купить продукты по списку ",
        repeat_weeks=3,
    )
    assert len(visits) == 4
    assert [v.visit_starts_at for v in visits] == [
        starts + datetime.timedelta(weeks=i) for i in range(4)
    ]
    assert visits[0].task_comment == "Купить продукты по списку"
    assert visits[0].default_hours == Decimal("2.00")
    with pytest.raises(ValidationError):
        services.create_visits(
            in_work_beneficiary, volunteer, starts_at=starts, ends_at=ends, repeat_weeks=13
        )


def test_fts05_visit_overlap_and_update(in_work_beneficiary, make_active, make_shift):
    volunteer = make_active()
    starts, ends = visit_times()
    (visit,) = services.create_visits(
        in_work_beneficiary, volunteer, starts_at=starts, ends_at=ends
    )
    with pytest.raises(ValidationError, match="уже есть назначение"):
        services.create_visits(
            in_work_beneficiary, volunteer, starts_at=starts + HOUR, ends_at=ends + HOUR
        )
    services.update_visit(
        visit, starts_at=starts + 3 * HOUR, ends_at=ends + 3 * HOUR, task_comment="Новое"
    )
    assert visit.visit_starts_at == starts + 3 * HOUR
    assert visit.task_comment == "Новое"


def test_fts05_case_pause_cancels_future_visits(in_work_beneficiary, make_active):
    starts, ends = visit_times()
    (visit,) = services.create_visits(
        in_work_beneficiary, make_active(), starts_at=starts, ends_at=ends
    )
    beneficiary_services.change_status(in_work_beneficiary, "paused")
    visit.refresh_from_db()
    assert visit.status == S.CANCELLED


def test_volunteer_left_cancels_future_assignments(make_shift, make_active, refs):
    volunteer = make_active()
    assignment = services.assign_to_shift(make_shift(), volunteer)
    volunteer_services.change_status(volunteer, "left", reason=refs["reason"])
    assignment.refresh_from_db()
    assert assignment.status == S.CANCELLED


def test_volunteer_archive_cancels_future_assignments(make_shift, make_active):
    volunteer = make_active()
    assignment = services.assign_to_shift(make_shift(), volunteer)
    volunteer_services.archive_volunteer(volunteer)
    assignment.refresh_from_db()
    assert assignment.status == S.CANCELLED


def test_removed_volunteer_gets_message(
    make_shift, make_active, django_capture_on_commit_callbacks
):
    volunteer = make_active()
    type(volunteer).objects.filter(pk=volunteer.pk).update(telegram_id=222)
    volunteer.refresh_from_db()
    assignment = services.assign_to_shift(make_shift(), volunteer)
    with (
        mock.patch("apps.shifts.tasks.notify_volunteer.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        services.change_assignment_status(assignment, S.CANCELLED, comment="Заболел")
    delay.assert_called_once_with(str(assignment.pk), "cancelled", 1)
