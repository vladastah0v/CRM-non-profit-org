"""Все изменения подопечных. Правила статусов и согласий проверяются только здесь (п. 4.3):
одинаково для веб-интерфейса, API и будущего бота."""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.roles import RoleCode, user_has_role
from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.consents.files import store_attachment
from apps.consents.models import Consent, ConsentKind, ConsentSigner
from apps.consents.services import revoke_consent
from apps.core.context import get_current_user
from apps.core.duplicates import PersonMatch, find_person_duplicates
from apps.core.models import CaseClosingReason, Program
from apps.core.normalize import normalize_email, normalize_phone
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from apps.shifts.services import cancel_for_beneficiary

from .models import (
    Beneficiary,
    BeneficiaryDocument,
    BeneficiarySensitive,
    CaseStatus,
    CaseStatusChange,
    CuratorAccess,
    RelatedPerson,
)

S = CaseStatus

# Допустимые переходы (ФТ-П-04, п. 4.3): новый → оценка → в работе, в работе ⇄ на паузе,
# любой → закрыт. «Закрыт» — конечный: повторное обращение — новая карточка.
TRANSITIONS: dict[str, frozenset[str]] = {
    S.NEW: frozenset({S.ASSESSMENT, S.CLOSED}),
    S.ASSESSMENT: frozenset({S.IN_WORK, S.CLOSED}),
    S.IN_WORK: frozenset({S.PAUSED, S.CLOSED}),
    S.PAUSED: frozenset({S.IN_WORK, S.CLOSED}),
    S.CLOSED: frozenset(),
}

EDITABLE_FIELDS: tuple[str, ...] = (
    "last_name",
    "first_name",
    "middle_name",
    "birth_date",
    "address",
    "phone",
    "category",
    "source",
)
SENSITIVE_FIELDS: tuple[str, ...] = ("health", "disability", "passport", "snils")
# Данные о здоровье — особая категория ПДн: нужно отдельное согласие (НФТ-ПД-03)
SPECIAL_CATEGORY_FIELDS = frozenset({"health", "disability"})


# ---------------------------------------------------------------------------
# Проверки
# ---------------------------------------------------------------------------
class DuplicateBeneficiaryError(ValidationError):
    """Найдены возможные дубли (ФТ-П-06). Сохранить можно только после подтверждения."""

    def __init__(self, matches: list[PersonMatch]) -> None:
        self.matches = matches
        super().__init__("Найдены похожие карточки подопечных.", code="duplicate")


def find_duplicates(
    *,
    last_name: str,
    first_name: str,
    middle_name: str = "",
    birth_date: datetime.date | None,
    phone: str = "",
    exclude_pk: Any = None,
) -> list[PersonMatch]:
    """ФТ-П-06: по ФИО с датой рождения и по телефону — включая архивные карточки."""
    return find_person_duplicates(
        Beneficiary.objects.select_related("curator"),
        last_name=last_name,
        first_name=first_name,
        middle_name=middle_name,
        birth_date=birth_date,
        phone=phone,
        exclude_pk=exclude_pk,
    )


def ensure_editable(beneficiary: Beneficiary) -> None:
    if beneficiary.is_archived:
        raise ValidationError("Карточка в архиве — сначала верните её из архива.")


def has_valid_consent(
    beneficiary: Beneficiary, kind: str = ConsentKind.GENERAL, day: datetime.date | None = None
) -> bool:
    """Действующее согласие; за несовершеннолетнего — подписанное представителем."""
    consents = Consent.objects.filter(beneficiary=beneficiary, kind=kind).valid_on(day)
    if beneficiary.is_minor:
        consents = consents.filter(signer=ConsentSigner.REPRESENTATIVE)
    return consents.exists()


def in_work_problems(beneficiary: Beneficiary) -> list[str]:
    """Чего не хватает для статуса «В работе» (п. 4.3). Пусто — можно."""
    problems = []
    if not has_valid_consent(beneficiary):
        if beneficiary.is_minor:
            problems.append("Нет действующего согласия, подписанного законным представителем.")
        else:
            problems.append("Нет действующего согласия на обработку персональных данных.")
    if (
        beneficiary.is_minor
        and not beneficiary.related_persons.filter(
            is_legal_representative=True, is_archived=False
        ).exists()
    ):
        problems.append("Не указан законный представитель несовершеннолетнего.")
    if beneficiary.curator_id is None:
        problems.append("Не назначен ответственный куратор.")
    return problems


def _ensure_consent_while_in_work(beneficiary: Beneficiary) -> None:
    """ФТ-Д-02: карточку в статусе «В работе» нельзя сохранить без действующего согласия."""
    if beneficiary.status == S.IN_WORK and not has_valid_consent(beneficiary):
        raise ValidationError(
            "Нельзя сохранить карточку в статусе «В работе» без действующего согласия. "
            "Добавьте согласие или переведите случай на паузу."
        )


def _check_curator(user: User | None) -> None:
    if user is not None and not (user.is_active and user_has_role(user, RoleCode.CURATOR)):
        raise ValidationError(
            {"curator": "Ответственным может быть только сотрудник с ролью «Куратор»."}
        )


# ---------------------------------------------------------------------------
# Карточка (ФТ-П-01)
# ---------------------------------------------------------------------------
def _normalized(data: dict[str, Any]) -> dict[str, Any]:
    clean = {key: data[key] for key in EDITABLE_FIELDS if key in data}
    for key in ("last_name", "first_name", "middle_name", "address"):
        if key in clean:
            clean[key] = " ".join((clean[key] or "").split())
    errors: dict[str, list[str]] = {}
    if "phone" in clean:
        try:
            clean["phone"] = normalize_phone(clean["phone"])
        except ValidationError as exc:
            errors["phone"] = exc.messages
    birth_date = clean.get("birth_date")
    if birth_date is not None and birth_date > timezone.localdate():
        errors["birth_date"] = ["Дата рождения не может быть в будущем."]
    for key in ("last_name", "first_name"):
        if key in clean and not clean[key]:
            errors[key] = ["Обязательное поле."]
    if errors:
        raise ValidationError(errors)
    return clean


def _record_status(
    beneficiary: Beneficiary,
    from_status: str,
    to_status: str,
    *,
    reason: CaseClosingReason | None = None,
    comment: str = "",
) -> None:
    CaseStatusChange.objects.create(
        beneficiary=beneficiary,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        comment=comment,
    )


def _notify_curator(beneficiary: Beneficiary, curator: User | None) -> None:
    """Новому ответственному — уведомление, если назначил его кто-то другой."""
    author = get_current_user()
    if curator is not None and (author is None or author.pk != curator.pk):
        notify(
            [curator],
            NotificationKind.CASE_ASSIGNED,
            "Вам назначен случай подопечного",
            url=beneficiary.get_absolute_url(),
        )


@transaction.atomic
def create_beneficiary(
    data: dict[str, Any],
    *,
    curator: User | None,
    programs: Iterable[Program] = (),
    confirm_duplicates: bool = False,
    request: HttpRequest | None = None,
) -> Beneficiary:
    """Новая карточка со статусом «Новый» (ФТ-П-01). Дубли — ФТ-П-06."""
    beneficiary = Beneficiary(**_normalized(data))
    if beneficiary.birth_date is None:
        raise ValidationError({"birth_date": ["Обязательное поле."]})
    _check_curator(curator)
    matches = find_duplicates(
        last_name=beneficiary.last_name,
        first_name=beneficiary.first_name,
        middle_name=beneficiary.middle_name,
        birth_date=beneficiary.birth_date,
        phone=beneficiary.phone,
    )
    if matches and not confirm_duplicates:
        raise DuplicateBeneficiaryError(matches)

    beneficiary.curator = curator
    beneficiary.save()
    beneficiary.programs.set(list(programs))
    _record_status(beneficiary, "", S.NEW)
    log_event(
        AuditAction.CREATE,
        request=request,
        obj=beneficiary,
        fields=[*data.keys(), "curator"],
        extra={"duplicates_confirmed": bool(matches)},
    )
    _notify_curator(beneficiary, curator)
    return beneficiary


@transaction.atomic
def update_beneficiary(
    beneficiary: Beneficiary,
    data: dict[str, Any],
    *,
    programs: Iterable[Program] | None = None,
    request: HttpRequest | None = None,
) -> Beneficiary:
    ensure_editable(beneficiary)
    _ensure_consent_while_in_work(beneficiary)
    clean = _normalized(data)
    changed = [key for key, value in clean.items() if getattr(beneficiary, key) != value]
    for key in changed:
        setattr(beneficiary, key, clean[key])
    beneficiary.save()
    if programs is not None:
        programs = list(programs)
        if {p.pk for p in programs} != set(beneficiary.programs.values_list("pk", flat=True)):
            beneficiary.programs.set(programs)
            changed.append("programs")
    if changed:
        log_event(AuditAction.UPDATE, request=request, obj=beneficiary, fields=changed)
    return beneficiary


# ---------------------------------------------------------------------------
# Кураторы и доступ к случаю (п. 2.3) — решает руководитель (администратор)
# ---------------------------------------------------------------------------
@transaction.atomic
def assign_curator(
    beneficiary: Beneficiary, curator: User | None, *, request: HttpRequest | None = None
) -> None:
    ensure_editable(beneficiary)
    _check_curator(curator)
    if curator is None and beneficiary.status == S.IN_WORK:
        raise ValidationError({"curator": "Случай в работе должен вести куратор."})
    if beneficiary.curator_id == (curator.pk if curator else None):
        return
    previous = beneficiary.curator_id
    beneficiary.curator = curator
    beneficiary.save(update_fields=["curator"])
    log_event(
        AuditAction.PERMISSION_CHANGE,
        request=request,
        obj=beneficiary,
        fields=["curator"],
        extra={"from": str(previous) if previous else "", "to": str(curator.pk) if curator else ""},
    )
    _notify_curator(beneficiary, curator)


@transaction.atomic
def grant_access(
    beneficiary: Beneficiary, user: User, *, request: HttpRequest | None = None
) -> CuratorAccess:
    """Открыть доступ к случаю второму куратору (п. 2.3)."""
    ensure_editable(beneficiary)
    _check_curator(user)
    if user.pk == beneficiary.curator_id:
        raise ValidationError({"user": "Это ответственный куратор — доступ у него уже есть."})
    access = CuratorAccess.objects.filter(beneficiary=beneficiary, user=user).active().first()
    if access is not None:
        raise ValidationError({"user": "У этого куратора уже есть доступ к случаю."})
    access = CuratorAccess.objects.create(beneficiary=beneficiary, user=user)
    log_event(
        AuditAction.PERMISSION_CHANGE,
        request=request,
        obj=beneficiary,
        fields=["curator_access"],
        extra={"granted": str(user.pk)},
    )
    _notify_curator(beneficiary, user)
    return access


@transaction.atomic
def revoke_access(access: CuratorAccess, *, request: HttpRequest | None = None) -> None:
    if access.is_archived:
        return
    access.archive()
    log_event(
        AuditAction.PERMISSION_CHANGE,
        request=request,
        obj=access.beneficiary,
        fields=["curator_access"],
        extra={"revoked": str(access.user_id)},
    )


# ---------------------------------------------------------------------------
# Статус случая (ФТ-П-04, п. 4.3)
# ---------------------------------------------------------------------------
def allowed_transitions(beneficiary: Beneficiary) -> list[str]:
    targets = TRANSITIONS[beneficiary.status]
    return [status for status in S.values if status in targets]


@transaction.atomic
def change_status(
    beneficiary: Beneficiary,
    to_status: str,
    *,
    reason: CaseClosingReason | None = None,
    outcome: str = "",
    comment: str = "",
    request: HttpRequest | None = None,
) -> Beneficiary:
    locked = Beneficiary.objects.select_for_update().get(pk=beneficiary.pk)
    from_status = locked.status
    ensure_editable(locked)
    if to_status not in S.values:
        raise ValidationError("Неизвестный статус.")
    if to_status not in TRANSITIONS[from_status]:
        raise ValidationError(
            f"Переход «{S(from_status).label}» → «{S(to_status).label}» не предусмотрен."
        )
    if to_status == S.IN_WORK:
        problems = in_work_problems(locked)
        if problems:
            raise ValidationError(problems)
    outcome = outcome.strip()
    if to_status == S.CLOSED:
        errors = {}
        if reason is None:
            errors["reason"] = "Укажите причину закрытия."
        if not outcome:
            errors["outcome"] = "Опишите итог работы со случаем."
        if errors:
            raise ValidationError(errors)

    locked.status = to_status
    locked.status_changed_at = timezone.now()
    fields = ["status", "status_changed_at"]
    if to_status == S.CLOSED:
        locked.closing_reason = reason
        locked.outcome = outcome
        locked.closed_on = timezone.localdate()
        fields += ["closing_reason", "outcome", "closed_on"]
    locked.save(update_fields=fields)
    if to_status in (S.CLOSED, S.PAUSED):
        # Данные подопечного больше не должны уходить волонтёрам (п. 2.3, ФТ-Д-02)
        cancel_for_beneficiary(locked, comment=f"Случай: «{S(to_status).label}»")
    _record_status(
        locked,
        from_status,
        to_status,
        reason=reason if to_status == S.CLOSED else None,
        comment=comment.strip(),
    )
    log_event(
        AuditAction.UPDATE,
        request=request,
        obj=locked,
        fields=fields,
        extra={"from": from_status, "to": to_status},
    )
    for name in fields:
        setattr(beneficiary, name, getattr(locked, name))
    return beneficiary


# ---------------------------------------------------------------------------
# Согласия (ФТ-Д-02, ФТ-Д-03)
# ---------------------------------------------------------------------------
@transaction.atomic
def revoke_beneficiary_consent(
    consent: Consent,
    *,
    revoked_on: datetime.date,
    note: str = "",
    request: HttpRequest | None = None,
) -> Consent:
    """Отзыв согласия. Если случай в работе и действующих согласий не осталось — он
    переводится на паузу: работать с данными без согласия нельзя (ФТ-Д-02)."""
    revoke_consent(consent, revoked_on=revoked_on, note=note, request=request)
    beneficiary = consent.beneficiary
    if (
        beneficiary is not None
        and beneficiary.status == S.IN_WORK
        and not has_valid_consent(beneficiary)
    ):
        change_status(
            beneficiary,
            S.PAUSED,
            comment="Автоматически: отозвано согласие на обработку данных.",
            request=request,
        )
    return consent


# ---------------------------------------------------------------------------
# Защищённый блок (ФТ-П-03)
# ---------------------------------------------------------------------------
_SNILS_RE = re.compile(r"^\d{11}$")


def normalize_snils(raw: str) -> str:
    """СНИЛС в формате 123-456-789 01 с проверкой контрольного числа."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if not _SNILS_RE.match(digits):
        raise ValidationError({"snils": "СНИЛС — 11 цифр."})
    number, control = digits[:9], int(digits[9:])
    total = sum(int(d) * (9 - i) for i, d in enumerate(number))
    expected = total if total < 100 else (0 if total in (100, 101) else total % 101 % 100)
    if int(number) > 1_001_998 and control != expected:
        raise ValidationError({"snils": "Неверное контрольное число СНИЛС — проверьте номер."})
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:9]} {digits[9:]}"


def get_sensitive(beneficiary: Beneficiary) -> BeneficiarySensitive:
    sensitive = BeneficiarySensitive.objects.filter(beneficiary=beneficiary).first()
    return sensitive or BeneficiarySensitive(beneficiary=beneficiary)


@transaction.atomic
def save_sensitive(
    beneficiary: Beneficiary, data: dict[str, str], *, request: HttpRequest | None = None
) -> BeneficiarySensitive:
    """Данные о здоровье и инвалидности — только при согласии на особые категории
    (НФТ-ПД-03). В журнал аудита — только названия изменённых полей."""
    ensure_editable(beneficiary)
    _ensure_consent_while_in_work(beneficiary)
    sensitive = get_sensitive(beneficiary)
    clean = {key: (data.get(key) or "").strip() for key in SENSITIVE_FIELDS if key in data}
    if "snils" in clean:
        clean["snils"] = normalize_snils(clean["snils"])
    changed = [key for key, value in clean.items() if getattr(sensitive, key) != value]
    needs_special = [key for key in changed if key in SPECIAL_CATEGORY_FIELDS and clean[key]]
    if needs_special and not has_valid_consent(beneficiary, ConsentKind.SPECIAL):
        message = (
            "Нужно действующее согласие на обработку особых категорий данных "
            "(здоровье) — добавьте его в карточке."
        )
        raise ValidationError(dict.fromkeys(needs_special, message))
    if not changed:
        return sensitive
    for key in changed:
        setattr(sensitive, key, clean[key])
    sensitive.save()
    log_event(
        AuditAction.UPDATE,
        request=request,
        obj=beneficiary,
        fields=changed,
        extra={"block": "sensitive"},
    )
    return sensitive


@transaction.atomic
def add_document(
    beneficiary: Beneficiary,
    *,
    kind: str,
    upload: UploadedFile,
    title: str = "",
    request: HttpRequest | None = None,
) -> BeneficiaryDocument:
    ensure_editable(beneficiary)
    document = BeneficiaryDocument.objects.create(
        beneficiary=beneficiary, kind=kind, title=title.strip(), file=store_attachment(upload)
    )
    log_event(
        AuditAction.CREATE,
        request=request,
        obj=document,
        extra={"beneficiary": str(beneficiary.pk), "kind": kind},
    )
    return document


# ---------------------------------------------------------------------------
# Связанные лица (ФТ-П-02)
# ---------------------------------------------------------------------------
def _person_data(data: dict[str, Any]) -> dict[str, Any]:
    clean = {
        "full_name": " ".join((data.get("full_name") or "").split()),
        "relation": " ".join((data.get("relation") or "").split()),
        "email": normalize_email(data.get("email", "")),
        "is_legal_representative": bool(data.get("is_legal_representative")),
    }
    errors: dict[str, list[str]] = {}
    try:
        clean["phone"] = normalize_phone(data.get("phone", ""))
    except ValidationError as exc:
        errors["phone"] = exc.messages
    for key in ("full_name", "relation"):
        if not clean[key]:
            errors[key] = ["Обязательное поле."]
    if errors:
        raise ValidationError(errors)
    return clean


@transaction.atomic
def add_related_person(
    beneficiary: Beneficiary, data: dict[str, Any], *, request: HttpRequest | None = None
) -> RelatedPerson:
    ensure_editable(beneficiary)
    person = RelatedPerson.objects.create(beneficiary=beneficiary, **_person_data(data))
    log_event(
        AuditAction.CREATE, request=request, obj=person, extra={"beneficiary": str(beneficiary.pk)}
    )
    return person


@transaction.atomic
def update_related_person(
    person: RelatedPerson, data: dict[str, Any], *, request: HttpRequest | None = None
) -> RelatedPerson:
    ensure_editable(person.beneficiary)
    clean = _person_data(data)
    changed = [key for key, value in clean.items() if getattr(person, key) != value]
    for key in changed:
        setattr(person, key, clean[key])
    if changed:
        person.save()
        log_event(AuditAction.UPDATE, request=request, obj=person, fields=changed)
    return person


@transaction.atomic
def archive_related_person(person: RelatedPerson, *, request: HttpRequest | None = None) -> None:
    ensure_editable(person.beneficiary)
    if not person.is_archived:
        person.archive()
        log_event(AuditAction.ARCHIVE, request=request, obj=person)


# ---------------------------------------------------------------------------
# Архив
# ---------------------------------------------------------------------------
@transaction.atomic
def archive_beneficiary(beneficiary: Beneficiary, *, request: HttpRequest | None = None) -> None:
    if not beneficiary.is_archived:
        beneficiary.archive()
        cancel_for_beneficiary(beneficiary, comment="Карточка подопечного в архиве")
        log_event(AuditAction.ARCHIVE, request=request, obj=beneficiary)


def restore_beneficiary(beneficiary: Beneficiary, *, request: HttpRequest | None = None) -> None:
    if beneficiary.is_archived:
        beneficiary.restore()
        log_event(AuditAction.RESTORE, request=request, obj=beneficiary)
