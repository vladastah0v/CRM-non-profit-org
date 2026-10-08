"""Напоминания координаторам: срок документа (ФТ-У-04) и волонтёры без смен (ФТ-У-05)."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.models import ShiftType, VolunteerDocumentType
from apps.notifications.models import Notification, NotificationKind
from apps.shifts.models import Assignment, AssignmentKind, AssignmentStatus, Shift
from apps.volunteers import activity, reminders, services
from apps.volunteers.models import Volunteer, VolunteerDocument

from .conftest import make_ready_for_activation

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate()


def days(n: int) -> datetime.date:
    return TODAY + datetime.timedelta(days=n)


@pytest.fixture
def med_book(db) -> VolunteerDocumentType:
    return VolunteerDocumentType.objects.get(name="Личная медицинская книжка")


def doc_notices() -> list[Notification]:
    return list(Notification.objects.filter(kind=NotificationKind.DOCUMENT_EXPIRING))


# --- Срок документа ---


def test_reminder_30_days_before_once(make_user, make_volunteer, med_book):
    coordinator = make_user("coordinator")
    volunteer = make_volunteer()
    services.add_document(volunteer, document_type=med_book, valid_until=days(30))
    services.add_document(make_volunteer(), document_type=med_book, valid_until=days(31))

    assert reminders.send_document_reminders() == 1
    (notice,) = doc_notices()
    assert notice.recipient == coordinator
    assert notice.title == "Истекает срок документа волонтёра"
    assert notice.body == f"Личная медицинская книжка. Действует до {days(30):%d.%m.%Y}."
    assert notice.url == volunteer.get_absolute_url()
    # В тексте нет ФИО волонтёра
    assert volunteer.last_name not in notice.title + notice.body
    # Повторный запуск — без повторного напоминания
    assert reminders.send_document_reminders() == 0


def test_expired_document_reminded_once(make_user, make_volunteer, med_book):
    make_user("coordinator")
    document = services.add_document(make_volunteer(), document_type=med_book)
    VolunteerDocument.objects.filter(pk=document.pk).update(valid_until=days(-3))
    assert reminders.send_document_reminders() == 1
    assert doc_notices()[0].title == "Истёк срок документа волонтёра"


def test_new_term_gets_new_reminder(make_user, make_volunteer, med_book):
    make_user("coordinator")
    document = services.add_document(make_volunteer(), document_type=med_book, valid_until=days(20))
    reminders.send_document_reminders()
    VolunteerDocument.objects.filter(pk=document.pk).update(valid_until=days(25))
    assert reminders.send_document_reminders() == 1


def test_replaced_document_is_not_reminded(make_user, make_volunteer, med_book):
    make_user("coordinator")
    volunteer = make_volunteer()
    services.add_document(volunteer, document_type=med_book, valid_until=days(10))
    services.add_document(volunteer, document_type=med_book, valid_until=days(375))
    assert reminders.send_document_reminders() == 0


def test_no_reminders_for_left_or_archived(make_user, make_volunteer, med_book, refs):
    make_user("coordinator")
    left = make_volunteer()
    services.add_document(left, document_type=med_book, valid_until=days(5))
    services.change_status(left, "left", reason=refs["reason"])
    archived = make_volunteer()
    services.add_document(archived, document_type=med_book, valid_until=days(5))
    services.archive_volunteer(archived)
    assert reminders.send_document_reminders() == 0


def test_document_reminder_goes_to_admins_without_coordinators(make_user, make_volunteer, med_book):
    admin = make_user("admin")
    services.add_document(make_volunteer(), document_type=med_book, valid_until=days(1))
    reminders.send_document_reminders()
    assert [n.recipient for n in doc_notices()] == [admin]


# --- Волонтёры без смен ---


def active_volunteer(make_volunteer, refs, *, active_days_ago: int) -> Volunteer:
    volunteer = make_ready_for_activation(make_volunteer(), refs)
    services.change_status(volunteer, "active")
    Volunteer.objects.filter(pk=volunteer.pk).update(
        status_changed_at=timezone.now() - datetime.timedelta(days=active_days_ago)
    )
    return volunteer


def attended(volunteer: Volunteer, *, days_ago: int) -> None:
    starts = timezone.now() - datetime.timedelta(days=days_ago)
    shift = Shift.objects.create(
        title="Раздача",
        shift_type=ShiftType.objects.get_or_create(name="Раздача")[0],
        starts_at=starts,
        ends_at=starts + datetime.timedelta(hours=3),
        address="Склад",
        volunteers_needed=5,
    )
    Assignment.objects.create(
        kind=AssignmentKind.SHIFT,
        volunteer=volunteer,
        shift=shift,
        status=AssignmentStatus.ATTENDED,
    )


def test_idle_digest(make_user, make_volunteer, refs):
    coordinator = make_user("coordinator")
    never = active_volunteer(make_volunteer, refs, active_days_ago=90)
    long_ago = active_volunteer(make_volunteer, refs, active_days_ago=200)
    attended(long_ago, days_ago=61)
    recent = active_volunteer(make_volunteer, refs, active_days_ago=200)
    attended(recent, days_ago=59)
    active_volunteer(make_volunteer, refs, active_days_ago=10)  # недавно стал активным
    back_from_pause = active_volunteer(make_volunteer, refs, active_days_ago=5)
    attended(back_from_pause, days_ago=120)

    assert reminders.send_idle_digest() == 2
    (notice,) = Notification.objects.filter(kind=NotificationKind.VOLUNTEERS_IDLE)
    assert notice.recipient == coordinator
    assert notice.title == "Волонтёры без смен больше 60 дней: 2"
    assert notice.url == f"{reverse('volunteers:list')}?last_shift=idle"
    idle = set(activity.idle(Volunteer.objects.all()).values_list("pk", flat=True))
    assert idle == {never.pk, long_ago.pk}


def test_idle_digest_skipped_when_nobody_idle(make_user, make_volunteer, refs):
    make_user("coordinator")
    active_volunteer(make_volunteer, refs, active_days_ago=10)
    assert reminders.send_idle_digest() == 0
    assert not Notification.objects.exists()


def test_list_filter_matches_digest(make_user, verified_client, make_volunteer, refs):
    idle = active_volunteer(make_volunteer, refs, active_days_ago=90)
    fresh = active_volunteer(make_volunteer, refs, active_days_ago=10)
    client = verified_client(make_user("coordinator"))
    content = client.get(reverse("volunteers:list"), {"last_shift": "idle"}).content.decode()
    assert idle.last_name in content
    assert fresh.last_name not in content
