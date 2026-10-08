"""Согласия на обработку ПДн (ФТ-Д-01, ФТ-Д-03). Все изменения — только через эти функции."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest
from django.utils import timezone

from apps.accounts.roles import RoleCode
from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify, users_with_role

from .files import store_attachment
from .models import Consent, ConsentChannel, ConsentKind, ConsentSigner

if TYPE_CHECKING:
    from apps.beneficiaries.models import Beneficiary
    from apps.volunteers.models import Volunteer


def create_consent(
    *,
    volunteer: Volunteer | None = None,
    beneficiary: Beneficiary | None = None,
    kind: str = ConsentKind.GENERAL,
    signed_on: datetime.date,
    signer: str = ConsentSigner.SUBJECT,
    signer_name: str = "",
    valid_until: datetime.date | None = None,
    channel: str = ConsentChannel.PAPER,
    scan: UploadedFile | None = None,
    request: HttpRequest | None = None,
) -> Consent:
    """ФТ-Д-01: тип, дата подписания, кто подписал, скан, срок действия."""
    if (volunteer is None) == (beneficiary is None):
        raise ValueError("Согласие относится ровно к одной карточке")
    signer_name = signer_name.strip()
    if signer == ConsentSigner.REPRESENTATIVE and not signer_name:
        raise ValidationError({"signer_name": "Укажите ФИО законного представителя."})
    if signed_on > timezone.localdate():
        raise ValidationError({"signed_on": "Дата подписания не может быть в будущем."})
    if valid_until is not None and valid_until < signed_on:
        raise ValidationError({"valid_until": "Срок действия заканчивается раньше подписания."})

    consent = Consent(
        volunteer=volunteer,
        beneficiary=beneficiary,
        kind=kind,
        signed_on=signed_on,
        signer=signer,
        signer_name=signer_name,
        valid_until=valid_until,
        channel=channel,
    )
    if scan is not None:
        consent.scan = store_attachment(scan)
    consent.save()
    subject: Any = volunteer or beneficiary
    log_event(
        AuditAction.CREATE,
        request=request,
        obj=consent,
        extra={subject._meta.model_name: str(subject.pk), "kind": kind},
    )
    return consent


def create_volunteer_consent(volunteer: Volunteer, **fields: Any) -> Consent:
    return create_consent(volunteer=volunteer, **fields)


def create_beneficiary_consent(beneficiary: Beneficiary, **fields: Any) -> Consent:
    return create_consent(beneficiary=beneficiary, **fields)


def revoke_consent(
    consent: Consent,
    *,
    revoked_on: datetime.date,
    note: str = "",
    request: HttpRequest | None = None,
) -> Consent:
    """ФТ-Д-03: отзыв фиксируется с датой. Если действующих согласий у человека не
    осталось — создаётся запрос на обезличивание со сроком 30 дней, администраторы
    получают уведомление. Обезличивает администратор (ФТ-А-04, apps/consents/anonymize.py).
    """
    if consent.revoked_on is not None:
        raise ValidationError("Согласие уже отозвано.")
    if revoked_on < consent.signed_on:
        raise ValidationError({"revoked_on": "Дата отзыва раньше даты подписания."})
    if revoked_on > timezone.localdate():
        raise ValidationError({"revoked_on": "Дата отзыва не может быть в будущем."})
    consent.revoked_on = revoked_on
    consent.revocation_note = note.strip()
    consent.save(update_fields=["revoked_on", "revocation_note"])
    log_event(
        AuditAction.UPDATE,
        request=request,
        obj=consent,
        fields=["revoked_on", "revocation_note"],
        extra={"revoked": True},
    )
    subject: Any = consent.volunteer or consent.beneficiary
    if _request_erasure(consent, subject):
        due = subject.erasure_deadline
        body = (
            "Действующих согласий не осталось: прекратите обработку и обезличьте данные "
            f"до {due:%d.%m.%Y}."
        )
    else:
        body = "Действуют другие согласия — проверьте, какие данные можно обрабатывать дальше."
    notify(
        users_with_role(RoleCode.ADMIN),
        NotificationKind.CONSENT_REVOKED,
        "Отозвано согласие на обработку данных",
        body=body,
        url=subject.get_absolute_url(),
    )
    return consent


def _request_erasure(consent: Consent, subject: Any) -> bool:
    """Запрос на обезличивание, если у человека не осталось действующих согласий."""
    if subject is None or subject.anonymized_at is not None:
        return False
    card = {"volunteer": subject} if consent.volunteer_id else {"beneficiary": subject}
    if Consent.objects.valid_on().filter(**card).exists():
        return False
    if subject.erasure_requested_on is None:
        subject.erasure_requested_on = timezone.localdate()
        subject.save(update_fields=["erasure_requested_on"])
    return True
