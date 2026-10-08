"""Напоминание об истекающем согласии (ФТ-У-04, Celery Beat): за 30 дней и по истечении.

Куратору — о согласиях его подопечных, координаторам — о согласиях волонтёров. В тексте —
вид согласия и срок, без ФИО.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from apps.accounts.roles import RoleCode
from apps.beneficiaries.models import CaseStatus
from apps.core.dates import EXPIRY_NOTICE_DAYS
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify, users_with_role
from apps.volunteers.models import VolunteerStatus

from .models import Consent


def _resigned() -> Exists:
    """У человека уже есть согласие того же вида, которое действует дольше."""
    return Exists(
        Consent.objects.filter(
            Q(volunteer=OuterRef("volunteer")) & Q(volunteer__isnull=False)
            | Q(beneficiary=OuterRef("beneficiary")) & Q(beneficiary__isnull=False),
            kind=OuterRef("kind"),
            is_archived=False,
            revoked_on__isnull=True,
        ).filter(Q(valid_until__isnull=True) | Q(valid_until__gt=OuterRef("valid_until")))
    )


def _active_subjects() -> Q:
    """Карточки, с которыми фонд работает: не в архиве, не обезличены, не выбыл и не закрыт."""
    volunteer = (
        Q(volunteer__isnull=False)
        & Q(volunteer__is_archived=False, volunteer__anonymized_at__isnull=True)
        & ~Q(volunteer__status=VolunteerStatus.LEFT)
    )
    beneficiary = (
        Q(beneficiary__isnull=False)
        & Q(beneficiary__is_archived=False, beneficiary__anonymized_at__isnull=True)
        & ~Q(beneficiary__status=CaseStatus.CLOSED)
    )
    return volunteer | beneficiary


def send_expiry_reminders(today: datetime.date | None = None) -> int:
    today = today or timezone.localdate()
    horizon = today + datetime.timedelta(days=EXPIRY_NOTICE_DAYS)
    due = (
        Consent.objects.filter(
            _active_subjects(),
            valid_until__lte=horizon,
            is_archived=False,
            revoked_on__isnull=True,
        )
        .exclude(expiry_notice_for=F("valid_until"))
        .exclude(_resigned())
        .select_related("volunteer", "beneficiary__curator")
    )
    staff: dict[str, list[Any]] = {}
    count = 0
    for consent in due:
        if consent.valid_until is None:
            continue
        subject: Any = consent.volunteer or consent.beneficiary
        expired = consent.valid_until < today
        notify(
            _recipients(consent, staff),
            NotificationKind.CONSENT_EXPIRING,
            "Истёк срок согласия" if expired else "Истекает срок согласия",
            body=f"{consent.get_kind_display()}. Действует до {consent.valid_until:%d.%m.%Y}.",
            url=subject.get_absolute_url(),
        )
        # update() — без записи в историю согласия: это служебная отметка
        Consent.objects.filter(pk=consent.pk).update(expiry_notice_for=consent.valid_until)
        count += 1
    return count


def _recipients(consent: Consent, staff: dict[str, list[Any]]) -> list[Any]:
    if consent.beneficiary is not None:
        curator = consent.beneficiary.curator
        if curator is not None and curator.is_active:
            return [curator]
        role = RoleCode.ADMIN  # случай без куратора — администраторам
    else:
        role = RoleCode.COORDINATOR
    if role not in staff:
        staff[role] = users_with_role(role) or users_with_role(RoleCode.ADMIN)
    return staff[role]
