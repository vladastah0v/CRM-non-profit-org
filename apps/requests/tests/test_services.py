"""Заявки и помощь: ФТ-З-01…03, ФТ-У-03, п. 4.3 ТЗ."""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.audit.models import AuditAction, AuditLog
from apps.beneficiaries import services as beneficiary_services
from apps.core.context import acting_as
from apps.notifications.models import Notification, NotificationKind
from apps.requests import services
from apps.requests.models import Assistance, HelpRequest, RequestStatus

from .conftest import TODAY, assistance_data

pytestmark = pytest.mark.django_db
S = RequestStatus


# --- ФТ-З-01 ----------------------------------------------------------------------------------


def test_ft_z_01_create_defaults_responsible_to_curator(make_request, curator, make_user):
    with acting_as(make_user("admin")):
        r = make_request()
    assert r.status == S.NEW and r.responsible == curator
    assert r.status_changes.get().to_status == S.NEW
    assert r.history.count() == 1
    assert AuditLog.objects.filter(action=AuditAction.CREATE, object_id=str(r.pk)).exists()
    note = Notification.objects.get(recipient=curator, kind=NotificationKind.REQUEST_ASSIGNED)
    assert note.url == r.get_absolute_url()


def test_no_notification_for_own_request(make_request, curator):
    with acting_as(curator):
        make_request()
    assert not Notification.objects.filter(kind=NotificationKind.REQUEST_ASSIGNED).exists()


def test_responsible_must_be_curator_or_admin(make_request, make_user):
    with pytest.raises(ValidationError) as exc:
        make_request(responsible=make_user("coordinator"))
    assert "responsible" in exc.value.message_dict


def test_no_requests_for_closed_or_archived_case(make_request, make_beneficiary, brefs):
    closed = make_beneficiary()
    beneficiary_services.change_status(closed, "closed", reason=brefs["reason"], outcome="Итог")
    with pytest.raises(ValidationError, match="закрыт"):
        make_request(closed)
    archived = make_beneficiary()
    beneficiary_services.archive_beneficiary(archived)
    with pytest.raises(ValidationError, match="архиве"):
        make_request(archived)


def test_description_required(make_request):
    with pytest.raises(ValidationError):
        make_request(description="   ")


# --- ФТ-З-02, п. 4.3 ---------------------------------------------------------------------------


def test_ft_z_02_transition_table_matches_spec():
    allowed = {(a, b) for a, targets in services.TRANSITIONS.items() for b in targets}
    assert allowed == {
        (S.NEW, S.IN_WORK),
        (S.IN_WORK, S.DONE),
        (S.NEW, S.REJECTED),
        (S.NEW, S.CANCELLED),
        (S.IN_WORK, S.REJECTED),
        (S.IN_WORK, S.CANCELLED),
    }


def test_done_requires_assistance(make_request, food, curator):
    r = make_request()
    services.change_status(r, S.IN_WORK)
    with pytest.raises(ValidationError, match="хотя бы один факт помощи"):
        services.change_status(r, S.DONE)
    services.register_assistance(r.beneficiary, assistance_data(food, curator), help_request=r)
    services.change_status(r, S.DONE)
    r.refresh_from_db()
    assert r.status == S.DONE and r.completed_at is not None
    assert services.allowed_transitions(r) == []


@pytest.mark.parametrize("target", [S.REJECTED, S.CANCELLED])
def test_rejection_needs_reason(target, make_request):
    r = make_request()
    with pytest.raises(ValidationError) as exc:
        services.change_status(r, target, reason="  ")
    assert "reason" in exc.value.message_dict
    services.change_status(r, target, reason="Помощь оказана другим фондом")
    r.refresh_from_db()
    assert r.resolution_reason == "Помощь оказана другим фондом"
    with pytest.raises(IntegrityError), transaction.atomic():
        HelpRequest.objects.filter(pk=r.pk).update(resolution_reason="")


def test_new_cannot_jump_to_done(make_request):
    with pytest.raises(ValidationError, match="не предусмотрен"):
        services.change_status(make_request(), S.DONE)


def test_update_only_open_and_resets_reminder(make_request):
    r = make_request()
    r.deadline_notified_on = TODAY()
    r.save()
    services.update_request(r, {"due_date": TODAY() + datetime.timedelta(days=30)})
    r.refresh_from_db()
    assert r.deadline_notified_on is None
    services.change_status(r, S.CANCELLED, reason="Не нужно")
    with pytest.raises(ValidationError, match="открытую"):
        services.update_request(r, {"description": "x"})


# --- ФТ-З-03 ----------------------------------------------------------------------------------


def test_ft_z_03_register_assistance(make_request, food, curator, brefs):
    r = make_request()
    a = services.register_assistance(
        r.beneficiary,
        assistance_data(
            food,
            curator,
            quantity=Decimal(2),
            unit="",
            amount=Decimal("1500.50"),
            program=brefs["program"],
        ),
        help_request=r,
    )
    assert a.unit == "шт." and a.amount == Decimal("1500.50") and a.request == r
    assert a.executor == curator and a.history.count() == 1


def test_assistance_by_volunteer(make_beneficiary, make_volunteer, food):
    volunteer = make_volunteer()
    a = services.register_assistance(
        make_beneficiary(),
        assistance_data(food, None, executor_volunteer=volunteer),
    )
    assert a.executor == volunteer


@pytest.mark.parametrize(
    ("fields", "error_field"),
    [
        ({"quantity": None, "amount": None}, "quantity"),
        ({"quantity": Decimal(-1)}, "quantity"),
        ({"amount": Decimal("-5")}, "amount"),
        ({"executor_user": None}, "executor_user"),
        ({"provided_on": TODAY() + datetime.timedelta(days=1)}, "provided_on"),
    ],
)
def test_assistance_validation(fields, error_field, make_beneficiary, food, curator):
    with pytest.raises(ValidationError) as exc:
        services.register_assistance(make_beneficiary(), assistance_data(food, curator, **fields))
    assert error_field in exc.value.message_dict


def test_both_executors_rejected(make_beneficiary, make_volunteer, food, curator):
    with pytest.raises(ValidationError):
        services.register_assistance(
            make_beneficiary(),
            assistance_data(food, curator, executor_volunteer=make_volunteer()),
        )


def test_database_checks(make_beneficiary, food, curator):
    a = services.register_assistance(make_beneficiary(), assistance_data(food, curator))
    for update in (
        {"quantity": Decimal(-1)},
        {"quantity": None, "amount": None},
        {"executor_user": None},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            Assistance.objects.filter(pk=a.pk).update(**update)


def test_assistance_request_must_match(make_request, make_beneficiary, food, curator):
    r = make_request()
    with pytest.raises(ValidationError) as exc:
        services.register_assistance(
            make_beneficiary(), assistance_data(food, curator), help_request=r
        )
    assert "request" in exc.value.message_dict


def test_last_assistance_of_done_request_cannot_be_archived(make_request, food, curator):
    r = make_request()
    services.change_status(r, S.IN_WORK)
    first = services.register_assistance(
        r.beneficiary, assistance_data(food, curator), help_request=r
    )
    services.change_status(r, S.DONE)
    with pytest.raises(ValidationError, match="единственный"):
        services.archive_assistance(first)


# --- ФТ-У-03: напоминания о сроке ---------------------------------------------------------------


def test_ft_u_03_deadline_reminders(make_request, curator, make_user):
    today = TODAY()
    tomorrow = make_request(due_date=today + datetime.timedelta(days=1))
    overdue = make_request(due_date=today - datetime.timedelta(days=2))
    make_request(due_date=today + datetime.timedelta(days=5))  # рано
    closed = make_request(due_date=today)
    services.change_status(closed, S.CANCELLED, reason="Не нужно")
    no_date = make_request(due_date=None)

    assert services.send_deadline_reminders(today) == 2
    titles = set(
        Notification.objects.filter(kind=NotificationKind.REQUEST_DUE).values_list(
            "title", flat=True
        )
    )
    assert titles == {"Завтра срок заявки", "Просрочена заявка"}
    assert services.send_deadline_reminders(today) == 0  # одно напоминание на срок
    tomorrow.refresh_from_db()
    overdue.refresh_from_db()
    assert tomorrow.deadline_notified_on == today and overdue.deadline_notified_on == today
    no_date.refresh_from_db()
    assert no_date.deadline_notified_on is None


def test_celery_task_runs(make_request):
    from apps.requests.tasks import send_deadline_reminders

    make_request(due_date=TODAY())
    assert send_deadline_reminders.delay().get() == 1
