"""Все изменения волонтёров. Правила переходов статусов проверяются только здесь (п. 4.3 ТЗ):
одинаково для веб-интерфейса, API приёма анкет и будущего Telegram-бота."""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.contrib.postgres.search import TrigramSimilarity
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpRequest
from django.utils import timezone

from apps.accounts.roles import RoleCode
from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.consents.files import PHOTO_TYPES, store_attachment
from apps.consents.models import Consent, ConsentChannel, ConsentKind, ConsentSigner
from apps.consents.services import create_volunteer_consent
from apps.core.models import (
    District,
    LeavingReason,
    Program,
    Skill,
    TrainingType,
    VolunteerSource,
)
from apps.core.normalize import normalize_email, normalize_name, normalize_phone
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify, users_with_role

from .models import (
    Volunteer,
    VolunteerAvailability,
    VolunteerHours,
    VolunteerStatus,
    VolunteerStatusChange,
    VolunteerTraining,
)

S = VolunteerStatus

# Допустимые переходы (ФТ-В-02, п. 4.3): анкета → собеседование → обучение → активен,
# активен ⇄ пауза, любой → выбыл. «Выбыл» — конечный статус: вернуть волонтёра можно
# только новой анкетой.
TRANSITIONS: dict[str, frozenset[str]] = {
    S.APPLICATION: frozenset({S.INTERVIEW, S.LEFT}),
    S.INTERVIEW: frozenset({S.TRAINING, S.LEFT}),
    S.TRAINING: frozenset({S.ACTIVE, S.LEFT}),
    S.ACTIVE: frozenset({S.PAUSED, S.LEFT}),
    S.PAUSED: frozenset({S.ACTIVE, S.LEFT}),
    S.LEFT: frozenset(),
}

# Порог похожести ФИО (pg_trgm) при совпадении даты рождения: ловит опечатки
# («Иванова Мария» / «Иванова Марья»), но не однофамильцев с другим именем.
NAME_SIMILARITY = 0.6
INTAKE_SOURCE_NAME = "Сайт фонда"

# Поля карточки, которые меняются через create/update (статус — только change_status)
EDITABLE_FIELDS: tuple[str, ...] = (
    "last_name",
    "first_name",
    "middle_name",
    "birth_date",
    "phone",
    "email",
    "telegram_username",
    "city",
    "district",
    "source",
    "guardian_name",
    "guardian_relation",
    "guardian_phone",
    "guardian_email",
)

AvailabilitySlot = tuple[int, datetime.time, datetime.time]


# ---------------------------------------------------------------------------
# Проверка дублей (ФТ-В-04)
# ---------------------------------------------------------------------------
@dataclass
class DuplicateMatch:
    volunteer: Volunteer
    reasons: list[str] = field(default_factory=list)


class DuplicateVolunteerError(ValidationError):
    """Найдены возможные дубли. Сохранить можно только после подтверждения."""

    def __init__(self, matches: list[DuplicateMatch]) -> None:
        self.matches = matches
        super().__init__("Найдены похожие карточки волонтёров.", code="duplicate")


def find_duplicates(
    *,
    phone: str,
    email: str = "",
    last_name: str,
    first_name: str,
    middle_name: str = "",
    birth_date: datetime.date | None,
    exclude_pk: Any = None,
    limit: int = 10,
) -> list[DuplicateMatch]:
    """Совпадение телефона, email или ФИО с датой рождения — включая архивные карточки."""
    name = normalize_name(last_name, first_name, middle_name)
    email = normalize_email(email)
    name_matches: set[Any] = set()
    if birth_date is not None and name:
        name_matches = set(
            Volunteer.objects.filter(birth_date=birth_date)
            .annotate(similarity=TrigramSimilarity("search_name", name))
            .filter(Q(search_name=name) | Q(similarity__gte=NAME_SIMILARITY))
            .values_list("pk", flat=True)
        )
    condition = Q(pk__in=name_matches)
    if phone:
        condition |= Q(phone=phone)
    if email:
        condition |= Q(email=email)

    candidates = Volunteer.objects.filter(condition).select_related("district")
    if exclude_pk is not None:
        candidates = candidates.exclude(pk=exclude_pk)

    matches = []
    for volunteer in candidates.order_by("-created_at")[:limit]:
        match = DuplicateMatch(volunteer)
        if phone and volunteer.phone == phone:
            match.reasons.append("телефон")
        if email and volunteer.email == email:
            match.reasons.append("email")
        if volunteer.pk in name_matches:
            match.reasons.append("ФИО и дата рождения")
        matches.append(match)
    return matches


def duplicates_of(volunteer: Volunteer) -> list[DuplicateMatch]:
    """Карточки, похожие на эту (для пометки «возможный дубль» в карточке)."""
    return find_duplicates(
        phone=volunteer.phone,
        email=volunteer.email,
        last_name=volunteer.last_name,
        first_name=volunteer.first_name,
        middle_name=volunteer.middle_name,
        birth_date=volunteer.birth_date,
        exclude_pk=volunteer.pk,
    )


@transaction.atomic
def resolve_duplicate(
    volunteer: Volunteer, *, same_person: bool, request: HttpRequest | None = None
) -> None:
    """Решение координатора по пометке «возможный дубль» (ФТ-В-04).

    - другой человек — пометка снимается, карточка остаётся в работе;
    - тот же человек — карточка уходит в архив (пометка остаётся: это и есть дубль).
      Нужные сведения координатор переносит в основную карточку сам.
    """
    ensure_editable(volunteer)
    if not volunteer.is_possible_duplicate:
        raise ValidationError("Карточка не помечена как возможный дубль.")
    if same_person:
        archive_volunteer(volunteer, request=request)
        decision = "same_person"
    else:
        volunteer.is_possible_duplicate = False
        volunteer.save(update_fields=["is_possible_duplicate"])
        decision = "different_person"
    log_event(
        AuditAction.UPDATE,
        request=request,
        obj=volunteer,
        fields=["is_possible_duplicate"],
        extra={"duplicate": decision},
    )


# ---------------------------------------------------------------------------
# Создание и изменение карточки (ФТ-В-01, ФТ-В-08)
# ---------------------------------------------------------------------------
def _normalized(data: dict[str, Any]) -> dict[str, Any]:
    """Нормализовать и проверить поля карточки. Ошибки — ValidationError по полям."""
    clean = {key: data[key] for key in EDITABLE_FIELDS if key in data}
    for key in ("last_name", "first_name", "middle_name", "city", "guardian_name"):
        if key in clean:
            clean[key] = " ".join((clean[key] or "").split())
    if "telegram_username" in clean:
        clean["telegram_username"] = (clean["telegram_username"] or "").strip().lstrip("@")
    for key in ("email", "guardian_email"):
        if key in clean:
            clean[key] = normalize_email(clean[key])

    errors: dict[str, list[str]] = {}
    for key in ("phone", "guardian_phone"):
        if key in clean:
            try:
                clean[key] = normalize_phone(clean[key])
            except ValidationError as exc:
                errors.setdefault(key, []).extend(exc.messages)
    birth_date = clean.get("birth_date")
    if birth_date is not None and birth_date > timezone.localdate():
        errors.setdefault("birth_date", []).append("Дата рождения не может быть в будущем.")
    if errors:
        raise ValidationError(errors)
    return clean


def ensure_editable(volunteer: Volunteer) -> None:
    """Архивная карточка только для чтения: сначала её нужно вернуть из архива."""
    if volunteer.is_archived:
        raise ValidationError("Карточка в архиве — сначала верните её из архива.")


def _check_required(volunteer: Volunteer) -> None:
    errors: dict[str, list[str]] = {}
    for key in ("last_name", "first_name", "phone"):
        if not getattr(volunteer, key):
            errors.setdefault(key, []).append("Обязательное поле.")
    if volunteer.birth_date is None:
        errors.setdefault("birth_date", []).append("Обязательное поле.")
    elif volunteer.is_minor:
        # ФТ-В-08: для несовершеннолетних данные законного представителя обязательны
        if not volunteer.guardian_name:
            errors.setdefault("guardian_name", []).append(
                "Волонтёру нет 18 лет — укажите законного представителя."
            )
        if not volunteer.guardian_phone:
            errors.setdefault("guardian_phone", []).append("Укажите телефон представителя.")
    if errors:
        raise ValidationError(errors)


def _set_availability(volunteer: Volunteer, slots: Iterable[AvailabilitySlot]) -> None:
    volunteer.availability.all().delete()
    rows = []
    for weekday, time_from, time_to in slots:
        if time_to <= time_from:
            raise ValidationError("Время «до» должно быть позже времени «с».")
        rows.append(
            VolunteerAvailability(
                volunteer=volunteer, weekday=weekday, time_from=time_from, time_to=time_to
            )
        )
    for row in rows:
        row.save()


def _record_status(
    volunteer: Volunteer,
    from_status: str,
    to_status: str,
    *,
    reason: LeavingReason | None = None,
    comment: str = "",
) -> None:
    VolunteerStatusChange.objects.create(
        volunteer=volunteer,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        comment=comment,
    )


@transaction.atomic
def create_volunteer(
    data: dict[str, Any],
    *,
    skills: Iterable[Skill] = (),
    programs: Iterable[Program] = (),
    availability: Iterable[AvailabilitySlot] = (),
    photo: UploadedFile | None = None,
    confirm_duplicates: bool = False,
    request: HttpRequest | None = None,
) -> Volunteer:
    """Новая карточка со статусом «Анкета» (ФТ-В-01). Дубли — ФТ-В-04."""
    volunteer = Volunteer(**_normalized(data))
    _check_required(volunteer)
    matches = find_duplicates(
        phone=volunteer.phone,
        email=volunteer.email,
        last_name=volunteer.last_name,
        first_name=volunteer.first_name,
        middle_name=volunteer.middle_name,
        birth_date=volunteer.birth_date,
    )
    if matches and not confirm_duplicates:
        raise DuplicateVolunteerError(matches)

    if photo is not None:
        volunteer.photo = store_attachment(photo, PHOTO_TYPES)
    volunteer.save()
    volunteer.skills.set(skills)
    volunteer.programs.set(programs)
    _set_availability(volunteer, availability)
    _record_status(volunteer, "", S.APPLICATION)
    log_event(
        AuditAction.CREATE,
        request=request,
        obj=volunteer,
        fields=[*data.keys()],
        extra={"duplicates_confirmed": bool(matches)},
    )
    return volunteer


@transaction.atomic
def update_volunteer(
    volunteer: Volunteer,
    data: dict[str, Any],
    *,
    skills: Iterable[Skill] | None = None,
    programs: Iterable[Program] | None = None,
    availability: Iterable[AvailabilitySlot] | None = None,
    photo: UploadedFile | None = None,
    remove_photo: bool = False,
    request: HttpRequest | None = None,
) -> Volunteer:
    ensure_editable(volunteer)
    clean = _normalized(data)
    changed = [key for key, value in clean.items() if getattr(volunteer, key) != value]
    for key in changed:
        setattr(volunteer, key, clean[key])
    _check_required(volunteer)

    if photo is not None:
        volunteer.photo = store_attachment(photo, PHOTO_TYPES)
        changed.append("photo")
    elif remove_photo and volunteer.photo_id is not None:
        volunteer.photo = None  # файл остаётся в хранилище: он может быть в истории
        changed.append("photo")
    volunteer.save()

    if skills is not None:
        skills = list(skills)
        if {s.pk for s in skills} != set(volunteer.skills.values_list("pk", flat=True)):
            volunteer.skills.set(skills)
            changed.append("skills")
    if programs is not None:
        programs = list(programs)
        if {p.pk for p in programs} != set(volunteer.programs.values_list("pk", flat=True)):
            volunteer.programs.set(programs)
            changed.append("programs")
    if availability is not None:
        slots = sorted(availability)
        current = sorted(volunteer.availability.values_list("weekday", "time_from", "time_to"))
        if slots != current:
            _set_availability(volunteer, slots)
            changed.append("availability")

    if changed:
        log_event(AuditAction.UPDATE, request=request, obj=volunteer, fields=changed)
    return volunteer


# ---------------------------------------------------------------------------
# Статусы (ФТ-В-02, п. 4.3)
# ---------------------------------------------------------------------------
def allowed_transitions(volunteer: Volunteer) -> list[str]:
    targets = TRANSITIONS[volunteer.status]
    return [status for status in S.values if status in targets]


def has_valid_consent(volunteer: Volunteer, day: datetime.date | None = None) -> bool:
    """Действующее согласие на обработку ПДн; у несовершеннолетнего — от представителя."""
    consents = Consent.objects.filter(volunteer=volunteer, kind=ConsentKind.GENERAL).valid_on(day)
    if volunteer.is_minor:
        consents = consents.filter(signer=ConsentSigner.REPRESENTATIVE)
    return consents.exists()


def activation_problems(volunteer: Volunteer) -> list[str]:
    """Чего не хватает для статуса «Активен» (п. 4.3, ФТ-В-08). Пусто — можно."""
    problems = []
    if volunteer.is_minor:
        if not (volunteer.guardian_name and volunteer.guardian_phone):
            problems.append("Не заполнены данные законного представителя.")
        if not has_valid_consent(volunteer):
            problems.append("Нет действующего согласия, подписанного законным представителем.")
    elif not has_valid_consent(volunteer):
        problems.append("Нет действующего согласия на обработку персональных данных.")
    if not volunteer.trainings.filter(is_archived=False).exists():
        problems.append("Не отмечено пройденное обучение.")
    return problems


@transaction.atomic
def change_status(
    volunteer: Volunteer,
    to_status: str,
    *,
    reason: LeavingReason | None = None,
    comment: str = "",
    request: HttpRequest | None = None,
) -> Volunteer:
    """Перевести волонтёра в новый статус с проверкой перехода и условий."""
    # Блокировка строки: два координатора не переведут волонтёра одновременно
    locked = Volunteer.objects.select_for_update().get(pk=volunteer.pk)
    from_status = locked.status
    ensure_editable(locked)
    if to_status not in S.values:
        raise ValidationError("Неизвестный статус.")
    if to_status not in TRANSITIONS[from_status]:
        raise ValidationError(
            f"Переход «{S(from_status).label}» → «{S(to_status).label}» не предусмотрен."
        )
    if to_status == S.ACTIVE:
        problems = activation_problems(locked)
        if problems:
            raise ValidationError(problems)
    if to_status == S.LEFT and reason is None:
        raise ValidationError({"reason": "Укажите причину выбытия."})
    if reason is not None and to_status != S.LEFT:
        reason = None  # причина нужна только для выбытия

    today = timezone.localdate()
    locked.status = to_status
    locked.status_changed_at = timezone.now()
    fields = ["status", "status_changed_at"]
    if to_status == S.ACTIVE and locked.start_date is None:
        locked.start_date = today
        fields.append("start_date")
    if to_status == S.LEFT:
        locked.leaving_reason = reason
        locked.left_on = today
        fields += ["leaving_reason", "left_on"]
    locked.save(update_fields=fields)
    _record_status(locked, from_status, to_status, reason=reason, comment=comment.strip())
    log_event(
        AuditAction.UPDATE,
        request=request,
        obj=locked,
        fields=fields,
        extra={"from": from_status, "to": to_status},
    )
    # Вызывающий код продолжает работать с актуальными данными
    for name in fields:
        setattr(volunteer, name, getattr(locked, name))
    return volunteer


# ---------------------------------------------------------------------------
# Обучение и часы (ФТ-В-06, ФТ-В-07)
# ---------------------------------------------------------------------------
def _not_in_future(day: datetime.date, field_name: str) -> None:
    if day > timezone.localdate():
        raise ValidationError({field_name: "Дата не может быть в будущем."})


@transaction.atomic
def add_training(
    volunteer: Volunteer,
    *,
    training_type: TrainingType,
    completed_on: datetime.date,
    conducted_by: str = "",
    request: HttpRequest | None = None,
) -> VolunteerTraining:
    ensure_editable(volunteer)
    _not_in_future(completed_on, "completed_on")
    training = VolunteerTraining.objects.create(
        volunteer=volunteer,
        training_type=training_type,
        completed_on=completed_on,
        conducted_by=" ".join(conducted_by.split()),
    )
    log_event(
        AuditAction.CREATE, request=request, obj=training, extra={"volunteer": str(volunteer.pk)}
    )
    return training


@transaction.atomic
def add_manual_hours(
    volunteer: Volunteer,
    *,
    worked_on: datetime.date,
    hours: Decimal,
    comment: str,
    program: Program | None = None,
    request: HttpRequest | None = None,
) -> VolunteerHours:
    """ФТ-В-07: часы вручную — только с комментарием."""
    ensure_editable(volunteer)
    _not_in_future(worked_on, "worked_on")
    comment = comment.strip()
    if not comment:
        raise ValidationError({"comment": "Объясните, за что начислены часы."})
    if not Decimal("0") < hours <= Decimal("24"):
        raise ValidationError({"hours": "Часы — больше 0 и не больше 24 за день."})
    record = VolunteerHours.objects.create(
        volunteer=volunteer, worked_on=worked_on, hours=hours, comment=comment, program=program
    )
    log_event(
        AuditAction.CREATE, request=request, obj=record, extra={"volunteer": str(volunteer.pk)}
    )
    return record


def total_hours(volunteer: Volunteer) -> Decimal:
    """Отработанные часы. Часы из смен добавятся с модулем «Смены» (этап 7)."""
    total = volunteer.manual_hours.filter(is_archived=False).aggregate(total=Sum("hours"))["total"]
    return total or Decimal("0")


# ---------------------------------------------------------------------------
# Архив
# ---------------------------------------------------------------------------
def archive_volunteer(volunteer: Volunteer, *, request: HttpRequest | None = None) -> None:
    if not volunteer.is_archived:
        volunteer.archive()
        log_event(AuditAction.ARCHIVE, request=request, obj=volunteer)


def restore_volunteer(volunteer: Volunteer, *, request: HttpRequest | None = None) -> None:
    if volunteer.is_archived:
        volunteer.restore()
        log_event(AuditAction.RESTORE, request=request, obj=volunteer)


# ---------------------------------------------------------------------------
# Анкета с сайта (ФТ-В-03, п. 7.1)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IntakeApplication:
    data: dict[str, Any]
    district_name: str = ""
    skill_names: Sequence[str] = ()
    availability: Sequence[AvailabilitySlot] = ()


def _match_by_name(model: type[Any], names: Iterable[str]) -> list[Any]:
    wanted = {normalize_name(n) for n in names if n and n.strip()}
    if not wanted:
        return []
    return [obj for obj in model.objects.active() if normalize_name(obj.name) in wanted]


def _coordinators() -> list[Any]:
    """Получатели уведомления о новой анкете; если координаторов нет — администраторы."""
    return users_with_role(RoleCode.COORDINATOR) or users_with_role(RoleCode.ADMIN)


@transaction.atomic
def create_from_intake(
    application: IntakeApplication, *, request: HttpRequest | None = None
) -> Volunteer:
    """Анкета с сайта: карточка «Анкета», согласие с отметкой времени, проверка дублей,
    уведомление координаторам. Карточка создаётся всегда; при совпадении — с пометкой
    «возможный дубль», решение принимает координатор."""
    clean = _normalized(application.data)
    volunteer = Volunteer(**clean)
    _check_required(volunteer)

    if application.district_name:
        districts = _match_by_name(District, [application.district_name])
        volunteer.district = districts[0] if len(districts) == 1 else None
    volunteer.source, _ = VolunteerSource.objects.get_or_create(name=INTAKE_SOURCE_NAME)
    volunteer.is_possible_duplicate = bool(duplicates_of(volunteer))
    volunteer.save()
    volunteer.skills.set(_match_by_name(Skill, application.skill_names))
    _set_availability(volunteer, application.availability)
    _record_status(volunteer, "", S.APPLICATION)

    # Отметка о согласии в форме сайта (п. 7.1): факт и время сохраняются.
    # За несовершеннолетнего согласие даёт законный представитель (ФТ-В-08).
    minor = volunteer.is_minor
    create_volunteer_consent(
        volunteer,
        kind=ConsentKind.GENERAL,
        signed_on=timezone.localdate(),
        signer=ConsentSigner.REPRESENTATIVE if minor else ConsentSigner.SUBJECT,
        signer_name=volunteer.guardian_name if minor else "",
        channel=ConsentChannel.WEBSITE,
        request=request,
    )
    log_event(
        AuditAction.CREATE,
        request=request,
        obj=volunteer,
        fields=sorted(clean),
        extra={"channel": "website", "possible_duplicate": volunteer.is_possible_duplicate},
    )
    body = "Возможный дубль — проверьте совпадения." if volunteer.is_possible_duplicate else ""
    notify(
        _coordinators(),
        NotificationKind.NEW_VOLUNTEER_APPLICATION,
        "Новая анкета волонтёра с сайта",
        body=body,
        url=volunteer.get_absolute_url(),
    )
    return volunteer
