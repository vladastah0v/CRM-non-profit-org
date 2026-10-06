"""Согласия на обработку ПДн (ФТ-Д-01, ФТ-Д-03). Все изменения — только через эти функции."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest
from django.utils import timezone

from apps.audit.models import AuditAction
from apps.audit.services import log_event

from .files import store_attachment
from .models import Consent, ConsentChannel, ConsentKind, ConsentSigner

if TYPE_CHECKING:
    from apps.volunteers.models import Volunteer


def create_volunteer_consent(
    volunteer: Volunteer,
    *,
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
    signer_name = signer_name.strip()
    if signer == ConsentSigner.REPRESENTATIVE and not signer_name:
        raise ValidationError({"signer_name": "Укажите ФИО законного представителя."})
    if signed_on > timezone.localdate():
        raise ValidationError({"signed_on": "Дата подписания не может быть в будущем."})
    if valid_until is not None and valid_until < signed_on:
        raise ValidationError({"valid_until": "Срок действия заканчивается раньше подписания."})

    consent = Consent(
        volunteer=volunteer,
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
    log_event(
        AuditAction.CREATE,
        request=request,
        obj=consent,
        extra={"volunteer": str(volunteer.pk), "kind": kind},
    )
    return consent


def revoke_consent(
    consent: Consent,
    *,
    revoked_on: datetime.date,
    note: str = "",
    request: HttpRequest | None = None,
) -> Consent:
    """ФТ-Д-03: отзыв фиксируется с датой. Обезличивание данных — этап 8 (ФТ-А-04)."""
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
    return consent
