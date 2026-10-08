"""Напоминание об истекающем согласии (ФТ-У-04): за 30 дней и по истечении."""

import datetime

import pytest
from django.utils import timezone

from apps.beneficiaries.models import Beneficiary
from apps.beneficiaries.tests.conftest import (  # noqa: F401
    brefs,
    curator,
    give_consent,
    make_beneficiary,
)
from apps.consents.models import Consent, ConsentKind
from apps.consents.reminders import send_expiry_reminders
from apps.consents.services import revoke_consent
from apps.core.models import CaseClosingReason
from apps.notifications.models import Notification, NotificationKind
from apps.volunteers.tests.conftest import give_consent as give_volunteer_consent

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate()


def days(n: int) -> datetime.date:
    return TODAY + datetime.timedelta(days=n)


def notices() -> list[Notification]:
    return list(Notification.objects.filter(kind=NotificationKind.CONSENT_EXPIRING))


def test_curator_reminded_about_beneficiary_consent(make_beneficiary, curator):  # noqa: F811
    beneficiary = make_beneficiary()
    give_consent(beneficiary, valid_until=days(30))
    give_consent(make_beneficiary(), valid_until=days(31))

    assert send_expiry_reminders() == 1
    (notice,) = notices()
    assert notice.recipient == curator
    assert notice.title == "Истекает срок согласия"
    assert notice.body == (f"На обработку персональных данных. Действует до {days(30):%d.%m.%Y}.")
    assert notice.url == beneficiary.get_absolute_url()
    assert beneficiary.last_name not in notice.title + notice.body
    assert send_expiry_reminders() == 0  # одно напоминание на срок


def test_consent_history_not_touched(make_beneficiary):  # noqa: F811
    consent = give_consent(make_beneficiary(), valid_until=days(10))
    before = Consent.history.filter(id=consent.pk).count()
    send_expiry_reminders()
    assert Consent.history.filter(id=consent.pk).count() == before


def test_expired_consent(make_beneficiary):  # noqa: F811
    consent = give_consent(make_beneficiary(), signed_on=days(-400), valid_until=days(-1))
    assert send_expiry_reminders() == 1
    assert notices()[0].title == "Истёк срок согласия"
    consent.refresh_from_db()
    assert consent.expiry_notice_for == days(-1)


def test_no_reminder_when_already_resigned(make_beneficiary):  # noqa: F811
    beneficiary = make_beneficiary()
    give_consent(beneficiary, signed_on=days(-300), valid_until=days(10))
    give_consent(beneficiary)  # новое бессрочное того же вида
    assert send_expiry_reminders() == 0


def test_other_kind_does_not_count_as_resigned(make_beneficiary):  # noqa: F811
    beneficiary = make_beneficiary()
    give_consent(beneficiary, valid_until=days(10))
    give_consent(beneficiary, kind=ConsentKind.PHOTO_VIDEO)
    assert send_expiry_reminders() == 1


def test_no_reminder_for_revoked_or_closed(make_beneficiary, make_user):  # noqa: F811
    make_user("admin")
    revoked = give_consent(make_beneficiary(), valid_until=days(10))
    revoke_consent(revoked, revoked_on=TODAY)
    closed = make_beneficiary()
    give_consent(closed, valid_until=days(10))
    Beneficiary.objects.filter(pk=closed.pk).update(
        status="closed",
        closing_reason=CaseClosingReason.objects.create(name="Помощь оказана"),
        outcome="Помощь оказана",
    )
    assert send_expiry_reminders() == 0


def test_beneficiary_without_curator_goes_to_admins(make_beneficiary, make_user):  # noqa: F811
    admin = make_user("admin")
    beneficiary = make_beneficiary(curator=None)
    give_consent(beneficiary, valid_until=days(5))
    send_expiry_reminders()
    assert [n.recipient for n in notices()] == [admin]


def test_volunteer_consent_goes_to_coordinators(make_user, make_volunteer):
    coordinator = make_user("coordinator")
    volunteer = make_volunteer()
    give_volunteer_consent(volunteer, valid_until=days(7))
    assert send_expiry_reminders() == 1
    (notice,) = notices()
    assert notice.recipient == coordinator
    assert notice.url == volunteer.get_absolute_url()
