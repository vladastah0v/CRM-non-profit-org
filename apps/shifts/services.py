"""Смены и назначения. Правила статусов назначения (п. 4.3) проверяются только здесь:
одинаково для веб-интерфейса и Telegram-бота."""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.roles import RoleCode, user_has_role
from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.beneficiaries.models import Beneficiary, CaseStatus
from apps.core.models import Skill
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify, users_with_role
from apps.volunteers.models import Volunteer, VolunteerStatus

from .models import (
    MAX_HOURS,
    OCCUPYING_STATUSES,
    PENDING_STATUSES,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    AssignmentStatusChange,
    Shift,
    ShiftStatus,
)

S = AssignmentStatus

# п. 4.3: назначен → подтвердил → пришёл / не пришёл. Дополнительно:
# - явку можно отметить и без подтверждения (волонтёр пришёл, не нажав «Приду»);
# - «Пришёл» ⇄ «Не пришёл» — исправление ошибочной отметки;
# - «Отказался» и «Снят» возвращаются в «Назначен» только новым назначением.
TRANSITIONS: dict[str, frozenset[str]] = {
    S.ASSIGNED: frozenset({S.CONFIRMED, S.DECLINED, S.CANCELLED, S.ATTENDED, S.NO_SHOW}),
    S.CONFIRMED: frozenset({S.DECLINED, S.CANCELLED, S.ATTENDED, S.NO_SHOW}),
    S.ATTENDED: frozenset({S.NO_SHOW}),
    S.NO_SHOW: frozenset({S.ATTENDED}),
    S.DECLINED: frozenset({S.ASSIGNED}),
    S.CANCELLED: frozenset({S.ASSIGNED}),
}
# Ответ на приглашение — до начала; явка — после начала
BEFORE_START = frozenset({S.CONFIRMED, S.DECLINED})
AFTER_START = frozenset({S.ATTENDED, S.NO_SHOW})

SHIFT_FIELDS = (
    "title",
    "shift_type",
    "starts_at",
    "ends_at",
    "address",
    "district",
    "volunteers_needed",
    "program",
    "description",
    "allow_signup",
)
MAX_SHIFT_DURATION = datetime.timedelta(days=3)
MAX_VISIT_DURATION = datetime.timedelta(hours=12)
MAX_REPEAT_WEEKS = 12
REMIND_DAY = datetime.timedelta(hours=24)
REMIND_SOON = datetime.timedelta(hours=2)
# Отметить явку через бота можно в течение двух суток после окончания
ATTENDANCE_WINDOW = datetime.timedelta(days=2)


# ---------------------------------------------------------------------------
# Общие проверки
# ---------------------------------------------------------------------------
def _local(value: datetime.datetime) -> str:
    return f"{timezone.localtime(value):%d.%m.%Y %H:%M}"


def _check_volunteer(volunteer: Volunteer) -> None:
    if volunteer.is_archived or volunteer.status != VolunteerStatus.ACTIVE:
        raise ValidationError(
            {"volunteer": "Назначать можно только волонтёров в статусе «Активен»."}
        )


def find_overlap(
    volunteer: Volunteer,
    starts: datetime.datetime,
    ends: datetime.datetime,
    *,
    exclude: Iterable[Any] = (),
) -> Assignment | None:
    """Назначение волонтёра, которое пересекается по времени с [starts, ends)."""
    return (
        Assignment.objects.active()
        .with_times()
        .filter(
            volunteer=volunteer,
            status__in=OCCUPYING_STATUSES,
            starts__lt=ends,
            ends__gt=starts,
        )
        .exclude(pk__in=list(exclude))
        .select_related("shift")
        .first()
    )


def _check_overlap(
    volunteer: Volunteer,
    starts: datetime.datetime,
    ends: datetime.datetime,
    *,
    exclude: Iterable[Any] = (),
) -> None:
    other = find_overlap(volunteer, starts, ends, exclude=exclude)
    if other is not None:
        raise ValidationError(
            f"В это время у волонтёра уже есть назначение: {other.title.lower()} "
            f"{_local(other.starts_at)}."
        )


def _record(
    assignment: Assignment,
    from_status: str,
    to_status: str,
    *,
    via_bot: bool = False,
    comment: str = "",
) -> None:
    AssignmentStatusChange.objects.create(
        assignment=assignment,
        from_status=from_status,
        to_status=to_status,
        via_bot=via_bot,
        comment=comment[:500],
    )


def _premark_reminders(assignment: Assignment) -> None:
    """Сообщение о назначении уже заменяет напоминание, если до начала меньше суток."""
    left = assignment.starts_at - timezone.now()
    now = timezone.now()
    if left <= REMIND_DAY:
        assignment.reminded_day_at = now
    if left <= REMIND_SOON:
        assignment.reminded_soon_at = now


def _reset_reminders(assignment: Assignment) -> None:
    assignment.reminded_day_at = None
    assignment.reminded_soon_at = None
    assignment.attendance_asked_at = None
    _premark_reminders(assignment)


def _send_to_volunteer(assignment: Assignment, event: str, *, count: int = 1) -> None:
    """Сообщение волонтёру в Telegram — после фиксации транзакции, через Celery."""
    from .tasks import notify_volunteer

    if assignment.volunteer.telegram_id is None or assignment.ends_at <= timezone.now():
        return
    pk = str(assignment.pk)
    transaction.on_commit(lambda: notify_volunteer.delay(pk, event, count))


# ---------------------------------------------------------------------------
# Смены (ФТ-С-01)
# ---------------------------------------------------------------------------
def _clean_shift(data: dict[str, Any]) -> dict[str, Any]:
    clean = {key: data[key] for key in SHIFT_FIELDS if key in data}
    for key in ("title", "address"):
        if key in clean:
            clean[key] = " ".join((clean[key] or "").split())
    if "description" in clean:
        clean["description"] = (clean["description"] or "").strip()
    errors: dict[str, str] = {}
    if "title" in clean and not clean["title"]:
        errors["title"] = "Укажите название смены."
    if "address" in clean and not clean["address"]:
        errors["address"] = "Укажите место."
    starts, ends = clean.get("starts_at"), clean.get("ends_at")
    if starts is not None and ends is not None:
        if ends <= starts:
            errors["ends_at"] = "Окончание должно быть позже начала."
        elif ends - starts > MAX_SHIFT_DURATION:
            errors["ends_at"] = "Смена длится не больше 3 суток."
    needed = clean.get("volunteers_needed")
    if needed is not None and needed < 1:
        errors["volunteers_needed"] = "Нужен хотя бы один волонтёр."
    if errors:
        raise ValidationError(errors)
    return clean


def _ensure_shift_editable(shift: Shift) -> None:
    if shift.is_archived:
        raise ValidationError("Смена в архиве.")
    if shift.is_cancelled:
        raise ValidationError("Смена отменена.")


@transaction.atomic
def create_shift(
    data: dict[str, Any], *, skills: Iterable[Skill] = (), request: HttpRequest | None = None
) -> Shift:
    clean = _clean_shift(data)
    shift = Shift.objects.create(**clean)
    shift.skills.set(list(skills))
    log_event(AuditAction.CREATE, request=request, obj=shift, fields=[*clean, "skills"])
    return shift


@transaction.atomic
def update_shift(
    shift: Shift,
    data: dict[str, Any],
    *,
    skills: Iterable[Skill] | None = None,
    request: HttpRequest | None = None,
) -> Shift:
    """Изменение смены. Новое время — волонтёрам сообщение и новые напоминания."""
    _ensure_shift_editable(shift)
    clean = _clean_shift({**{key: getattr(shift, key) for key in SHIFT_FIELDS}, **data})
    changed = [key for key, value in clean.items() if getattr(shift, key) != value]
    for key in changed:
        setattr(shift, key, clean[key])
    if skills is not None:
        new_skills = list(skills)
        if {skill.pk for skill in new_skills} != set(shift.skills.values_list("pk", flat=True)):
            shift.skills.set(new_skills)
            changed.append("skills")
    if not changed:
        return shift
    shift.save()
    log_event(AuditAction.UPDATE, request=request, obj=shift, fields=changed)
    if {"starts_at", "ends_at"} & set(changed):
        for assignment in shift.assignments.filter(
            status__in=PENDING_STATUSES, is_archived=False
        ).select_related("volunteer"):
            assignment.shift = shift
            _reset_reminders(assignment)
            assignment.save(
                update_fields=["reminded_day_at", "reminded_soon_at", "attendance_asked_at"]
            )
            _send_to_volunteer(assignment, "changed")
    return shift


@transaction.atomic
def cancel_shift(shift: Shift, *, reason: str, request: HttpRequest | None = None) -> Shift:
    """Отмена смены: ожидающие назначения снимаются, волонтёрам — сообщение."""
    locked = Shift.objects.select_for_update().get(pk=shift.pk)
    _ensure_shift_editable(locked)
    reason = " ".join(reason.split())
    if not reason:
        raise ValidationError({"reason": "Укажите причину отмены."})
    locked.status = ShiftStatus.CANCELLED
    locked.cancel_reason = reason
    locked.save(update_fields=["status", "cancel_reason"])
    for assignment in locked.assignments.filter(
        status__in=PENDING_STATUSES, is_archived=False
    ).select_related("volunteer"):
        from_status = assignment.status
        assignment.status = S.CANCELLED
        assignment.status_changed_at = timezone.now()
        assignment.save(update_fields=["status", "status_changed_at"])
        _record(assignment, from_status, S.CANCELLED, comment="Смена отменена")
        assignment.shift = locked
        _send_to_volunteer(assignment, "cancelled")
    log_event(AuditAction.UPDATE, request=request, obj=locked, fields=["status", "cancel_reason"])
    shift.status, shift.cancel_reason = locked.status, locked.cancel_reason
    return shift


@transaction.atomic
def archive_shift(shift: Shift, *, request: HttpRequest | None = None) -> None:
    """Ошибочная смена — в архив вместе с назначениями (их часы перестают учитываться)."""
    if shift.is_archived:
        return
    for assignment in shift.assignments.filter(is_archived=False):
        assignment.archive()
    shift.archive()
    log_event(AuditAction.ARCHIVE, request=request, obj=shift)


# ---------------------------------------------------------------------------
# Назначения на смену (ФТ-С-02)
# ---------------------------------------------------------------------------
def occupied_places(shift: Shift) -> int:
    return shift.assignments.filter(status__in=OCCUPYING_STATUSES, is_archived=False).count()


def missing_skills(shift: Shift, volunteer: Volunteer) -> list[Skill]:
    have = set(volunteer.skills.values_list("pk", flat=True))
    return [skill for skill in shift.skills.all() if skill.pk not in have]


@transaction.atomic
def assign_to_shift(
    shift: Shift,
    volunteer: Volunteer,
    *,
    via_bot: bool = False,
    request: HttpRequest | None = None,
) -> Assignment:
    """Назначение координатором или запись волонтёра через бота (ФТ-С-02).

    Координатор может назначить и на прошедшую смену — чтобы отметить явку тех, кто
    пришёл без записи, — и сверх нужного числа. Через бота — только на будущую смену
    с открытой записью, свободными местами и всеми требуемыми навыками.
    """
    # Блокировка смены: двое не займут последнее место одновременно
    locked = Shift.objects.select_for_update().get(pk=shift.pk)
    _ensure_shift_editable(locked)
    _check_volunteer(volunteer)
    if via_bot:
        if not locked.allow_signup:
            raise ValidationError("На эту смену записывает координатор.")
        if locked.has_started:
            raise ValidationError("Смена уже началась.")
        if missing_skills(locked, volunteer):
            raise ValidationError("Для этой смены нужны навыки, которых нет в вашей карточке.")
        if occupied_places(locked) >= locked.volunteers_needed:
            raise ValidationError("Свободных мест на смене больше нет.")

    existing = Assignment.objects.filter(shift=locked, volunteer=volunteer).first()
    if existing is not None and existing.status in OCCUPYING_STATUSES:
        raise ValidationError("Волонтёр уже записан на эту смену.")
    _check_overlap(
        volunteer,
        locked.starts_at,
        locked.ends_at,
        exclude=[existing.pk] if existing is not None else [],
    )

    if existing is None:
        assignment = Assignment(kind=AssignmentKind.SHIFT, shift=locked, volunteer=volunteer)
        from_status = ""
    else:
        assignment = existing
        assignment.shift = locked
        from_status = assignment.status
        assignment.is_archived = False
        assignment.archived_at = None
    assignment.status = S.ASSIGNED
    assignment.status_changed_at = timezone.now()
    assignment.hours = None
    _reset_reminders(assignment)
    assignment.save()
    _record(assignment, from_status, S.ASSIGNED, via_bot=via_bot)
    log_event(
        AuditAction.CREATE if existing is None else AuditAction.UPDATE,
        request=request,
        obj=assignment,
        fields=["status"],
        extra={"shift": str(locked.pk), "volunteer": str(volunteer.pk), "via_bot": via_bot},
    )
    if not via_bot:
        _send_to_volunteer(assignment, "assigned")
    return assignment


# ---------------------------------------------------------------------------
# Визиты к подопечным (ФТ-С-05)
# ---------------------------------------------------------------------------
def _check_visit_beneficiary(beneficiary: Beneficiary) -> None:
    """Данные подопечного уходят волонтёру — только по случаю «в работе», то есть
    с действующим согласием и куратором (ФТ-Д-02)."""
    if beneficiary.is_archived or beneficiary.status != CaseStatus.IN_WORK:
        raise ValidationError("Назначать визиты можно только по случаю в статусе «В работе».")


def _check_visit_time(starts: datetime.datetime, ends: datetime.datetime) -> None:
    if ends <= starts:
        raise ValidationError({"time_to": "Окончание визита должно быть позже начала."})
    if ends - starts > MAX_VISIT_DURATION:
        raise ValidationError({"time_to": "Визит длится не больше 12 часов."})


@transaction.atomic
def create_visits(
    beneficiary: Beneficiary,
    volunteer: Volunteer,
    *,
    starts_at: datetime.datetime,
    ends_at: datetime.datetime,
    task_comment: str = "",
    program: Any = None,
    repeat_weeks: int = 0,
    request: HttpRequest | None = None,
) -> list[Assignment]:
    """Визит или серия еженедельных визитов (регулярные визиты, ФТ-С-05)."""
    _check_visit_beneficiary(beneficiary)
    _check_volunteer(volunteer)
    _check_visit_time(starts_at, ends_at)
    if not 0 <= repeat_weeks <= MAX_REPEAT_WEEKS:
        raise ValidationError({"repeat_weeks": f"Повторить можно до {MAX_REPEAT_WEEKS} недель."})
    task_comment = task_comment.strip()
    visits: list[Assignment] = []
    for week in range(repeat_weeks + 1):
        shift_by = datetime.timedelta(weeks=week)
        starts, ends = starts_at + shift_by, ends_at + shift_by
        _check_overlap(volunteer, starts, ends)
        visit = Assignment(
            kind=AssignmentKind.VISIT,
            volunteer=volunteer,
            beneficiary=beneficiary,
            visit_starts_at=starts,
            visit_ends_at=ends,
            task_comment=task_comment,
            program=program,
        )
        _premark_reminders(visit)
        visit.save()
        _record(visit, "", S.ASSIGNED)
        log_event(
            AuditAction.CREATE,
            request=request,
            obj=visit,
            extra={"beneficiary": str(beneficiary.pk), "volunteer": str(volunteer.pk)},
        )
        visits.append(visit)
    _send_to_volunteer(visits[0], "assigned", count=len(visits))
    return visits


@transaction.atomic
def update_visit(
    visit: Assignment,
    *,
    starts_at: datetime.datetime,
    ends_at: datetime.datetime,
    task_comment: str,
    program: Any = None,
    request: HttpRequest | None = None,
) -> Assignment:
    if not visit.is_visit or visit.is_archived or not visit.is_pending:
        raise ValidationError("Изменить можно только предстоящий визит.")
    if visit.has_started:
        raise ValidationError("Визит уже начался — отметьте явку.")
    _check_visit_time(starts_at, ends_at)
    values = {
        "visit_starts_at": starts_at,
        "visit_ends_at": ends_at,
        "task_comment": task_comment.strip(),
        "program": program,
    }
    changed = [key for key, value in values.items() if getattr(visit, key) != value]
    if not changed:
        return visit
    if {"visit_starts_at", "visit_ends_at"} & set(changed):
        _check_overlap(visit.volunteer, starts_at, ends_at, exclude=[visit.pk])
    for key in changed:
        setattr(visit, key, values[key])
    if {"visit_starts_at", "visit_ends_at"} & set(changed):
        _reset_reminders(visit)
        _send_to_volunteer(visit, "changed")
    visit.save()
    log_event(AuditAction.UPDATE, request=request, obj=visit, fields=changed)
    return visit


# ---------------------------------------------------------------------------
# Статусы назначения, явка и часы (ФТ-С-04)
# ---------------------------------------------------------------------------
def _clean_hours(hours: Decimal | None, default: Decimal) -> Decimal:
    value = default if hours is None else hours
    if not Decimal(0) < value <= MAX_HOURS:
        raise ValidationError({"hours": "Часы — больше 0 и не больше 24."})
    return value.quantize(Decimal("0.01"))


def allowed_transitions(assignment: Assignment) -> list[str]:
    """Переходы для интерфейса сотрудника (возврат в «Назначен» — новым назначением)."""
    if assignment.is_archived or (assignment.shift is not None and assignment.shift.is_cancelled):
        return []
    started = assignment.has_started
    targets = TRANSITIONS[assignment.status] - {S.ASSIGNED}
    return [
        status
        for status in S.values
        if status in targets
        and not (started and status in BEFORE_START)
        and not (not started and status in AFTER_START)
    ]


def _notify_declined(assignment: Assignment) -> None:
    """Волонтёр отказался через бота — тому, кто планировал смену или визит, иначе
    всем координаторам. Без ФИО: ссылка на смену или визит."""
    planner = assignment.shift.created_by if assignment.shift is not None else assignment.created_by
    if (
        planner is not None
        and planner.is_active
        and user_has_role(planner, RoleCode.COORDINATOR, RoleCode.ADMIN)
    ):
        recipients: list[User] = [planner]
    else:
        recipients = users_with_role(RoleCode.COORDINATOR)
    what = f"смены «{assignment.title}»" if not assignment.is_visit else "визита к подопечному"
    notify(
        recipients,
        NotificationKind.ASSIGNMENT_DECLINED,
        "Волонтёр отказался от участия",
        body=f"Отказ от {what} {_local(assignment.starts_at)}.",
        url=assignment.get_absolute_url(),
    )


@transaction.atomic
def change_assignment_status(
    assignment: Assignment,
    to_status: str,
    *,
    hours: Decimal | None = None,
    comment: str = "",
    via_bot: bool = False,
    request: HttpRequest | None = None,
) -> Assignment:
    """Подтверждение, отказ, отметка явки и часов (ФТ-С-04).

    Часы — только для «Пришёл»; по умолчанию — длительность смены или визита.
    Волонтёр в боте отвечает только на ожидающее назначение: отметку координатора
    он не меняет.
    """
    locked = (
        Assignment.objects.select_for_update(of=("self",))
        .select_related("shift", "volunteer")
        .get(pk=assignment.pk)
    )
    from_status = locked.status
    if locked.is_archived:
        raise ValidationError("Назначение в архиве.")
    if locked.shift is not None and locked.shift.is_cancelled:
        raise ValidationError("Смена отменена.")
    if to_status not in S.values:
        raise ValidationError("Неизвестный статус.")
    if to_status == S.ASSIGNED:
        raise ValidationError("Чтобы вернуть волонтёра, назначьте его заново.")
    if to_status not in TRANSITIONS[from_status]:
        raise ValidationError(
            f"Переход «{S(from_status).label}» → «{S(to_status).label}» не предусмотрен."
        )
    if via_bot and from_status not in PENDING_STATUSES:
        raise ValidationError("Отметка уже внесена координатором.")
    started = locked.has_started
    if to_status in BEFORE_START and started:
        what = "Визит уже начался" if locked.is_visit else "Смена уже началась"
        raise ValidationError(f"{what} — теперь отмечается явка.")
    if to_status in AFTER_START and not started:
        raise ValidationError("Явку можно отметить после начала.")
    if hours is not None and to_status != S.ATTENDED:
        raise ValidationError({"hours": "Часы вносятся только для статуса «Пришёл»."})

    locked.status = to_status
    locked.status_changed_at = timezone.now()
    locked.hours = _clean_hours(hours, locked.default_hours) if to_status == S.ATTENDED else None
    locked.save(update_fields=["status", "status_changed_at", "hours"])
    _record(locked, from_status, to_status, via_bot=via_bot, comment=comment.strip())
    log_event(
        AuditAction.UPDATE,
        request=request,
        obj=locked,
        fields=["status", "hours"] if to_status in AFTER_START else ["status"],
        extra={"from": from_status, "to": to_status, "via_bot": via_bot},
    )
    if to_status == S.DECLINED and via_bot:
        _notify_declined(locked)
    if to_status == S.CANCELLED:
        _send_to_volunteer(locked, "cancelled")  # координатор снял волонтёра
    for name in ("status", "status_changed_at", "hours"):
        setattr(assignment, name, getattr(locked, name))
    return assignment


@transaction.atomic
def update_hours(
    assignment: Assignment, hours: Decimal, *, request: HttpRequest | None = None
) -> Assignment:
    if assignment.is_archived or assignment.status != S.ATTENDED:
        raise ValidationError({"hours": "Часы вносятся только для статуса «Пришёл»."})
    value = _clean_hours(hours, assignment.default_hours)
    if value != assignment.hours:
        assignment.hours = value
        assignment.save(update_fields=["hours"])
        log_event(AuditAction.UPDATE, request=request, obj=assignment, fields=["hours"])
    return assignment


@transaction.atomic
def record_attendance(
    assignment: Assignment,
    *,
    attended: bool,
    hours: Decimal | None = None,
    request: HttpRequest | None = None,
) -> Assignment:
    """Одна строка листа явки: пришёл (с часами) или не пришёл."""
    if attended and assignment.status == S.ATTENDED:
        return update_hours(assignment, hours or assignment.default_hours, request=request)
    if not attended and assignment.status == S.NO_SHOW:
        return assignment
    return change_assignment_status(
        assignment,
        S.ATTENDED if attended else S.NO_SHOW,
        hours=hours if attended else None,
        request=request,
    )


# ---------------------------------------------------------------------------
# Снятие назначений при выбытии волонтёра и закрытии случая
# ---------------------------------------------------------------------------
def _cancel_pending(assignments: Sequence[Assignment], comment: str) -> int:
    for assignment in assignments:
        from_status = assignment.status
        assignment.status = S.CANCELLED
        assignment.status_changed_at = timezone.now()
        assignment.save(update_fields=["status", "status_changed_at"])
        _record(assignment, from_status, S.CANCELLED, comment=comment)
        log_event(
            AuditAction.UPDATE,
            obj=assignment,
            fields=["status"],
            extra={"from": from_status, "to": S.CANCELLED},
        )
    return len(assignments)


def _future_pending() -> Any:
    return (
        Assignment.objects.active()
        .with_times()
        .filter(status__in=PENDING_STATUSES, starts__gt=timezone.now())
        .select_related("shift", "volunteer")
    )


@transaction.atomic
def cancel_for_volunteer(volunteer: Volunteer, *, comment: str) -> int:
    """Волонтёр выбыл или карточка в архиве — предстоящие назначения снимаются."""
    return _cancel_pending(list(_future_pending().filter(volunteer=volunteer)), comment)


@transaction.atomic
def cancel_for_beneficiary(beneficiary: Beneficiary, *, comment: str) -> int:
    """Случай закрыт, на паузе (в т. ч. отзыв согласия) или в архиве — визиты снимаются:
    данные подопечного больше не должны уходить волонтёру."""
    visits = list(_future_pending().filter(beneficiary=beneficiary))
    for visit in visits:
        _send_to_volunteer(visit, "cancelled")
    return _cancel_pending(visits, comment)
