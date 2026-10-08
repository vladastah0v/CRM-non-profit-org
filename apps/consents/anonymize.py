"""Обезличивание данных человека по запросу (ФТ-А-04) и после отзыва согласия (ФТ-Д-03).

Что происходит:
- ФИО заменяются на «Обезличено <код>», из даты рождения остаётся только год (для
  возрастной статистики), стираются контакты, адрес, Telegram, представители;
- удаляются файлы (фото, сканы согласий, документы), защищённый блок подопечного,
  связанные лица, доступность волонтёра;
- тексты заявок, взаимодействий, задач, комментарии к визитам и помощи заменяются;
- удаляются копии в таблицах истории (django-simple-history) — кроме записи о самом
  обезличивании;
- остаются для статистики (сценарий 9.2): факты помощи (тип, дата, количество, сумма,
  программа), часы и отметки явки, статусы и их журнал, категория, район, навыки.

Карточка уходит в архив и больше не возвращается. В журнал аудита — событие ANONYMIZE
с основанием, без значений полей.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import RoleCode
from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.beneficiaries.models import (
    Beneficiary,
    BeneficiaryDocument,
    BeneficiarySensitive,
    CaseStatus,
    CaseStatusChange,
    RelatedPerson,
)
from apps.interactions.models import Interaction, Task
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify, users_with_role
from apps.requests.models import Assistance, HelpRequest, RequestStatusChange
from apps.shifts.models import Assignment, AssignmentStatusChange
from apps.shifts.services import cancel_for_beneficiary, cancel_for_volunteer
from apps.volunteers.models import (
    Volunteer,
    VolunteerHours,
    VolunteerStatus,
    VolunteerStatusChange,
)

from .files import delete_attachment
from .models import Attachment, Consent

ANONYMIZED_NAME = "Обезличено"
ERASED_TEXT = "[обезличено]"

BASIS_CHOICES = [
    ("revocation", "Отзыв согласия"),
    ("request", "Заявление субъекта или представителя"),
    ("retention", "Истёк срок хранения"),
    ("other", "Иное основание"),
]

Person = Volunteer | Beneficiary


def deadline(person: Person) -> datetime.date | None:
    return person.erasure_deadline


def _delete_files(attachments: list[Attachment | None]) -> None:
    for attachment in attachments:
        if attachment is not None:
            delete_attachment(attachment)


def _erase_consents(consents: Any) -> None:
    """Согласия остаются как факт (вид, даты), подписант и сканы стираются."""
    scans = []
    for consent in consents:
        scans.append(consent.scan)
        consent.signer_name = ""
        consent.revocation_note = ""
        consent.scan = None
        consent.save(update_fields=["signer_name", "revocation_note", "scan"])
    _delete_files(scans)


def _erase_card_notes(**card: Any) -> None:
    Interaction.objects.filter(**card).update(text=ERASED_TEXT)
    Task.objects.filter(**card).update(text=ERASED_TEXT)


def _anonymous_name(person: Person) -> tuple[str, str]:
    return ANONYMIZED_NAME, person.pk.hex[:6].upper()


def _check(person: Person, basis: str, document: str) -> str:
    if person.anonymized_at is not None:
        raise ValidationError("Данные уже обезличены.")
    if basis not in dict(BASIS_CHOICES):
        raise ValidationError({"basis": "Выберите основание."})
    document = " ".join(document.split())
    if not document:
        raise ValidationError({"document": "Укажите документ-основание: номер и дату."})
    return document[:100]


def _finish(
    person: Person, now: datetime.datetime, basis: str, document: str, request: Any
) -> None:
    person.is_archived = True
    person.archived_at = person.archived_at or now
    person.anonymized_at = now
    person.erasure_requested_on = None
    person.is_possible_duplicate = False
    person.save()
    # Копии данных в истории — удалить; остаётся только запись о самом обезличивании
    type(person).history.model.objects.filter(id=person.pk, history_date__lt=now).delete()
    log_event(
        AuditAction.ANONYMIZE,
        request=request,
        obj=person,
        extra={"basis": basis, "document": document},
    )


@transaction.atomic
def anonymize_volunteer(
    volunteer: Volunteer, *, basis: str, document: str, request: HttpRequest | None = None
) -> Volunteer:
    document = _check(volunteer, basis, document)
    volunteer = Volunteer.objects.select_for_update().get(pk=volunteer.pk)
    _check(volunteer, basis, document)  # повторно — под блокировкой: два запроса одновременно
    now = timezone.now()
    cancel_for_volunteer(volunteer, comment="Данные волонтёра обезличены")

    photo = volunteer.photo
    volunteer.last_name, volunteer.first_name = _anonymous_name(volunteer)
    volunteer.middle_name = ""
    volunteer.birth_date = datetime.date(volunteer.birth_date.year, 1, 1)
    for name in ("phone", "email", "telegram_username", "city", "bot_link_code"):
        setattr(volunteer, name, "")
    for name in ("guardian_name", "guardian_relation", "guardian_phone", "guardian_email"):
        setattr(volunteer, name, "")
    volunteer.telegram_id = None
    volunteer.bot_link_code_expires = None
    volunteer.photo = None
    if volunteer.user is not None and volunteer.user.is_active:
        volunteer.user.is_active = False
        volunteer.user.save(update_fields=["is_active"])

    volunteer.availability.all().delete()
    VolunteerHours.objects.filter(volunteer=volunteer).update(comment=ANONYMIZED_NAME)
    VolunteerStatusChange.objects.filter(volunteer=volunteer).update(comment="")
    consents = Consent.objects.filter(volunteer=volunteer).select_related("scan")
    _erase_consents(consents)
    Consent.history.model.objects.filter(volunteer_id=volunteer.pk).delete()
    Assignment.objects.filter(volunteer=volunteer).update(task_comment="")
    AssignmentStatusChange.objects.filter(assignment__volunteer=volunteer).update(comment="")
    Assignment.history.model.objects.filter(volunteer_id=volunteer.pk).delete()
    _erase_card_notes(volunteer=volunteer)

    _finish(volunteer, now, basis, document, request)
    _delete_files([photo])
    return volunteer


@transaction.atomic
def anonymize_beneficiary(
    beneficiary: Beneficiary, *, basis: str, document: str, request: HttpRequest | None = None
) -> Beneficiary:
    document = _check(beneficiary, basis, document)
    beneficiary = Beneficiary.objects.select_for_update().get(pk=beneficiary.pk)
    _check(beneficiary, basis, document)  # повторно — под блокировкой
    now = timezone.now()
    cancel_for_beneficiary(beneficiary, comment="Данные подопечного обезличены")

    beneficiary.last_name, beneficiary.first_name = _anonymous_name(beneficiary)
    beneficiary.middle_name = ""
    beneficiary.birth_date = datetime.date(beneficiary.birth_date.year, 1, 1)
    beneficiary.address = ""
    beneficiary.phone = ""
    # У закрытого случая итог обязателен (ограничение в базе) — текст заменяется
    beneficiary.outcome = ERASED_TEXT if beneficiary.outcome else ""

    # Защищённый блок (здоровье, инвалидность, документы) — удаляется целиком
    BeneficiarySensitive.objects.filter(beneficiary=beneficiary).delete()
    BeneficiarySensitive.history.model.objects.filter(beneficiary_id=beneficiary.pk).delete()
    documents = list(
        BeneficiaryDocument.objects.filter(beneficiary=beneficiary).select_related("file")
    )
    BeneficiaryDocument.objects.filter(pk__in=[d.pk for d in documents]).delete()
    _delete_files([d.file for d in documents])
    RelatedPerson.objects.filter(beneficiary=beneficiary).delete()
    CaseStatusChange.objects.filter(beneficiary=beneficiary).update(comment="")

    consents = Consent.objects.filter(beneficiary=beneficiary).select_related("scan")
    _erase_consents(consents)
    Consent.history.model.objects.filter(beneficiary_id=beneficiary.pk).delete()

    # Заявки и помощь: тексты стираются, факты остаются в статистике
    HelpRequest.objects.filter(beneficiary=beneficiary).update(description=ERASED_TEXT)
    HelpRequest.objects.filter(beneficiary=beneficiary).exclude(resolution_reason="").update(
        resolution_reason=ERASED_TEXT
    )
    RequestStatusChange.objects.filter(request__beneficiary=beneficiary).update(comment="")
    HelpRequest.history.model.objects.filter(beneficiary_id=beneficiary.pk).delete()
    Assistance.objects.filter(beneficiary=beneficiary).update(comment="")
    Assistance.history.model.objects.filter(beneficiary_id=beneficiary.pk).delete()

    Assignment.objects.filter(beneficiary=beneficiary).update(task_comment="")
    AssignmentStatusChange.objects.filter(assignment__beneficiary=beneficiary).update(comment="")
    Assignment.history.model.objects.filter(beneficiary_id=beneficiary.pk).delete()
    _erase_card_notes(beneficiary=beneficiary)

    _finish(beneficiary, now, basis, document, request)
    return beneficiary


# ---------------------------------------------------------------------------
# Запросы на обезличивание (ФТ-Д-03)
# ---------------------------------------------------------------------------
@transaction.atomic
def dismiss_erasure(person: Person, *, reason: str, request: HttpRequest | None = None) -> None:
    """Снять запрос: у фонда есть иное законное основание хранить данные (152-ФЗ, ст. 21)."""
    reason = " ".join(reason.split())
    if not reason:
        raise ValidationError({"reason": "Укажите основание, по которому данные сохраняются."})
    if person.erasure_requested_on is None:
        raise ValidationError("Запроса на обезличивание нет.")
    person.erasure_requested_on = None
    person.save(update_fields=["erasure_requested_on"])
    log_event(
        AuditAction.UPDATE,
        request=request,
        obj=person,
        fields=["erasure_requested_on"],
        extra={"erasure": "dismissed", "reason": reason[:200]},
    )


def pending_requests() -> list[Person]:
    pending = Q(erasure_requested_on__isnull=False, anonymized_at__isnull=True)
    people: list[Person] = [
        *Volunteer.objects.filter(pending),
        *Beneficiary.objects.filter(pending),
    ]
    return sorted(people, key=lambda p: (p.erasure_requested_on, p.last_name))


# ---------------------------------------------------------------------------
# Срок хранения (НФТ-ПД-06, п. 10.2): ежемесячно, автоматически, с отчётом администратору
# ---------------------------------------------------------------------------
def expired(today: datetime.date | None = None) -> tuple[list[Volunteer], list[Beneficiary]]:
    """Выбывшие волонтёры и закрытые случаи, у которых истёк срок хранения."""
    days = settings.PD_RETENTION_DAYS
    if days <= 0:
        return [], []
    cutoff = (today or timezone.localdate()) - datetime.timedelta(days=days)
    volunteers = Volunteer.objects.filter(
        status=VolunteerStatus.LEFT, left_on__lt=cutoff, anonymized_at__isnull=True
    )
    beneficiaries = Beneficiary.objects.filter(
        status=CaseStatus.CLOSED, closed_on__lt=cutoff, anonymized_at__isnull=True
    )
    return list(volunteers), list(beneficiaries)


def anonymize_expired(today: datetime.date | None = None) -> dict[str, int]:
    days = settings.PD_RETENTION_DAYS
    admins = users_with_role(RoleCode.ADMIN)
    if days <= 0:
        notify(
            admins,
            NotificationKind.RETENTION_REPORT,
            "Срок хранения данных не настроен",
            body="Данные выбывших волонтёров и закрытых случаев не обезличиваются "
            "автоматически: утвердите срок и задайте PD_RETENTION_DAYS (НФТ-ПД-06).",
        )
        return {"volunteers": 0, "beneficiaries": 0, "errors": 0}
    volunteers, beneficiaries = expired(today)
    document = f"Истёк срок хранения ({days} дн.)"
    done = {"volunteers": 0, "beneficiaries": 0, "errors": 0}
    for volunteer in volunteers:
        try:
            anonymize_volunteer(volunteer, basis="retention", document=document)
            done["volunteers"] += 1
        except ValidationError:
            done["errors"] += 1
    for beneficiary in beneficiaries:
        try:
            anonymize_beneficiary(beneficiary, basis="retention", document=document)
            done["beneficiaries"] += 1
        except ValidationError:
            done["errors"] += 1
    notify(
        admins,
        NotificationKind.RETENTION_REPORT,
        "Обезличивание по сроку хранения",
        body=f"Обезличено: волонтёров — {done['volunteers']}, подопечных — "
        f"{done['beneficiaries']}. Ошибок: {done['errors']}.",
        url=reverse("consents:erasure_list"),
    )
    return done
