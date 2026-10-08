"""Заявки и оказанная помощь. Правила статусов (п. 4.3) проверяются только здесь."""

from __future__ import annotations

import datetime
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
from apps.core.context import get_current_user
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify

from .models import Assistance, HelpRequest, RequestStatus, RequestStatusChange

S = RequestStatus

# ФТ-З-02, п. 4.3: новая → в работе → выполнена; новая или в работе → отклонена / отменена.
# Выполненная, отклонённая и отменённая заявка — конечные: новая потребность — новая заявка.
TRANSITIONS: dict[str, frozenset[str]] = {
    S.NEW: frozenset({S.IN_WORK, S.REJECTED, S.CANCELLED}),
    S.IN_WORK: frozenset({S.DONE, S.REJECTED, S.CANCELLED}),
    S.DONE: frozenset(),
    S.REJECTED: frozenset(),
    S.CANCELLED: frozenset(),
}
NEEDS_REASON = frozenset({S.REJECTED, S.CANCELLED})

REQUEST_FIELDS = ("assistance_type", "description", "urgency", "due_date")
ASSISTANCE_FIELDS = (
    "assistance_type",
    "provided_on",
    "quantity",
    "unit",
    "amount",
    "program",
    "executor_user",
    "executor_volunteer",
    "comment",
)


def _ensure_case_open(beneficiary: Beneficiary) -> None:
    if beneficiary.is_archived:
        raise ValidationError("Карточка подопечного в архиве.")
    if beneficiary.status == CaseStatus.CLOSED:
        raise ValidationError("Случай подопечного закрыт.")


def _check_responsible(user: User | None) -> None:
    if user is not None and not (
        user.is_active and user_has_role(user, RoleCode.CURATOR, RoleCode.ADMIN)
    ):
        raise ValidationError(
            {"responsible": "Ответственным может быть куратор или администратор."}
        )


def _notify_responsible(help_request: HelpRequest) -> None:
    """ФТ-У-03: ответственному — о новой заявке, если назначил его другой сотрудник."""
    author = get_current_user()
    responsible = help_request.responsible
    if responsible is not None and (author is None or author.pk != responsible.pk):
        notify(
            [responsible],
            NotificationKind.REQUEST_ASSIGNED,
            "Вам назначена заявка",
            body=f"Тип помощи: {help_request.assistance_type}.",
            url=help_request.get_absolute_url(),
        )


def _clean_request(data: dict[str, Any]) -> dict[str, Any]:
    clean = {key: data[key] for key in REQUEST_FIELDS if key in data}
    if "description" in clean:
        clean["description"] = (clean["description"] or "").strip()
        if not clean["description"]:
            raise ValidationError({"description": "Опишите, какая помощь нужна."})
    return clean


# ---------------------------------------------------------------------------
# Заявки (ФТ-З-01, ФТ-З-02)
# ---------------------------------------------------------------------------
@transaction.atomic
def create_request(
    beneficiary: Beneficiary,
    data: dict[str, Any],
    *,
    responsible: User | None = None,
    request: HttpRequest | None = None,
) -> HelpRequest:
    """Новая заявка. Ответственный по умолчанию — куратор подопечного."""
    _ensure_case_open(beneficiary)
    responsible = responsible or beneficiary.curator
    _check_responsible(responsible)
    help_request = HelpRequest(beneficiary=beneficiary, responsible=responsible)
    for key, value in _clean_request(data).items():
        setattr(help_request, key, value)
    if not help_request.description:
        raise ValidationError({"description": "Опишите, какая помощь нужна."})
    help_request.save()
    RequestStatusChange.objects.create(request=help_request, to_status=S.NEW)
    log_event(
        AuditAction.CREATE,
        request=request,
        obj=help_request,
        fields=[*data.keys(), "responsible"],
        extra={"beneficiary": str(beneficiary.pk)},
    )
    _notify_responsible(help_request)
    return help_request


@transaction.atomic
def update_request(
    help_request: HelpRequest,
    data: dict[str, Any],
    *,
    responsible: User | None = None,
    request: HttpRequest | None = None,
) -> HelpRequest:
    if not help_request.is_open or help_request.is_archived:
        raise ValidationError("Изменять можно только открытую заявку.")
    clean = _clean_request(data)
    changed = [key for key, value in clean.items() if getattr(help_request, key) != value]
    for key in changed:
        setattr(help_request, key, clean[key])
    new_responsible = responsible is not None and responsible.pk != help_request.responsible_id
    if new_responsible:
        _check_responsible(responsible)
        help_request.responsible = responsible
        help_request.deadline_notified_on = None
        changed.append("responsible")
    if "due_date" in changed:
        help_request.deadline_notified_on = None  # новый срок — новое напоминание
    if changed:
        help_request.save()
        log_event(AuditAction.UPDATE, request=request, obj=help_request, fields=changed)
    if new_responsible:
        _notify_responsible(help_request)
    return help_request


def allowed_transitions(help_request: HelpRequest) -> list[str]:
    targets = TRANSITIONS[help_request.status]
    return [status for status in S.values if status in targets]


@transaction.atomic
def change_status(
    help_request: HelpRequest,
    to_status: str,
    *,
    reason: str = "",
    comment: str = "",
    request: HttpRequest | None = None,
) -> HelpRequest:
    locked = HelpRequest.objects.select_for_update().get(pk=help_request.pk)
    from_status = locked.status
    if locked.is_archived:
        raise ValidationError("Заявка в архиве.")
    if to_status not in S.values:
        raise ValidationError("Неизвестный статус.")
    if to_status not in TRANSITIONS[from_status]:
        raise ValidationError(
            f"Переход «{S(from_status).label}» → «{S(to_status).label}» не предусмотрен."
        )
    reason = reason.strip()
    if to_status in NEEDS_REASON and not reason:
        raise ValidationError({"reason": "Укажите причину."})
    if to_status == S.DONE and not locked.assistance.filter(is_archived=False).exists():
        raise ValidationError(
            "Чтобы закрыть заявку как выполненную, зарегистрируйте хотя бы один факт помощи."
        )

    locked.status = to_status
    locked.status_changed_at = timezone.now()
    fields = ["status", "status_changed_at"]
    if to_status in NEEDS_REASON:
        locked.resolution_reason = reason
        fields.append("resolution_reason")
    if to_status == S.DONE:
        locked.completed_at = timezone.now()
        fields.append("completed_at")
    locked.save(update_fields=fields)
    RequestStatusChange.objects.create(
        request=locked, from_status=from_status, to_status=to_status, comment=comment.strip()
    )
    log_event(
        AuditAction.UPDATE,
        request=request,
        obj=locked,
        fields=fields,
        extra={"from": from_status, "to": to_status},
    )
    for name in fields:
        setattr(help_request, name, getattr(locked, name))
    return help_request


def archive_request(help_request: HelpRequest, *, request: HttpRequest | None = None) -> None:
    if not help_request.is_archived:
        help_request.archive()
        log_event(AuditAction.ARCHIVE, request=request, obj=help_request)


DEADLINE_NOTICE_DAYS = 1


def send_deadline_reminders(today: datetime.date | None = None) -> int:
    """ФТ-У-03: ответственному — о приближении срока заявки (за день) и о просрочке.

    Одно напоминание на срок: при переносе срока или смене ответственного — новое.
    """
    today = today or timezone.localdate()
    horizon = today + datetime.timedelta(days=DEADLINE_NOTICE_DAYS)
    due = HelpRequest.objects.open().filter(
        due_date__lte=horizon, deadline_notified_on__isnull=True, responsible__isnull=False
    )
    count = 0
    for help_request in due.select_related("responsible", "assistance_type"):
        if help_request.due_date is None or help_request.responsible is None:
            continue  # исключено фильтром; проверка — для типов
        if help_request.due_date < today:
            title = "Просрочена заявка"
        elif help_request.due_date == today:
            title = "Сегодня срок заявки"
        else:
            title = "Завтра срок заявки"
        notify(
            [help_request.responsible],
            NotificationKind.REQUEST_DUE,
            title,
            body=f"Тип помощи: {help_request.assistance_type}. "
            f"Срок — {help_request.due_date:%d.%m.%Y}.",
            url=help_request.get_absolute_url(),
        )
        help_request.deadline_notified_on = today
        help_request.save(update_fields=["deadline_notified_on"])
        count += 1
    return count


# ---------------------------------------------------------------------------
# Оказанная помощь (ФТ-З-03)
# ---------------------------------------------------------------------------
def _clean_assistance(
    beneficiary: Beneficiary, data: dict[str, Any], help_request: HelpRequest | None
) -> dict[str, Any]:
    clean = {key: data[key] for key in ASSISTANCE_FIELDS if key in data}
    errors: dict[str, list[str]] = {}

    provided_on: datetime.date | None = clean.get("provided_on")
    if provided_on is not None and provided_on > timezone.localdate():
        errors["provided_on"] = ["Дата не может быть в будущем."]
    quantity: Decimal | None = clean.get("quantity")
    amount: Decimal | None = clean.get("amount")
    if quantity is None and amount is None:
        errors.setdefault("quantity", []).append("Укажите количество или сумму.")
    for key, value in (("quantity", quantity), ("amount", amount)):
        if value is not None and value < 0:
            errors.setdefault(key, []).append("Не может быть отрицательным.")

    executor_user: User | None = clean.get("executor_user")
    executor_volunteer = clean.get("executor_volunteer")
    if (executor_user is None) == (executor_volunteer is None):
        errors.setdefault("executor_user", []).append(
            "Укажите одного исполнителя: сотрудника или волонтёра."
        )
    elif executor_user is not None and not executor_user.is_active:
        errors.setdefault("executor_user", []).append("Сотрудник заблокирован.")
    elif executor_volunteer is not None and executor_volunteer.is_archived:
        errors.setdefault("executor_volunteer", []).append("Карточка волонтёра в архиве.")

    if help_request is not None:
        if help_request.beneficiary_id != beneficiary.pk:
            errors.setdefault("request", []).append("Заявка другого подопечного.")
        elif not help_request.is_open:
            errors.setdefault("request", []).append("Заявка уже закрыта.")
    if errors:
        raise ValidationError(errors)

    assistance_type = clean.get("assistance_type")
    if not (clean.get("unit") or "").strip() and assistance_type is not None:
        clean["unit"] = assistance_type.unit  # единица — из справочника типов помощи
    clean["comment"] = (clean.get("comment") or "").strip()
    return clean


@transaction.atomic
def register_assistance(
    beneficiary: Beneficiary,
    data: dict[str, Any],
    *,
    help_request: HelpRequest | None = None,
    request: HttpRequest | None = None,
) -> Assistance:
    _ensure_case_open(beneficiary)
    clean = _clean_assistance(beneficiary, data, help_request)
    assistance = Assistance.objects.create(beneficiary=beneficiary, request=help_request, **clean)
    log_event(
        AuditAction.CREATE,
        request=request,
        obj=assistance,
        extra={"beneficiary": str(beneficiary.pk)},
    )
    return assistance


@transaction.atomic
def update_assistance(
    assistance: Assistance, data: dict[str, Any], *, request: HttpRequest | None = None
) -> Assistance:
    if assistance.is_archived:
        raise ValidationError("Запись в архиве.")
    clean = _clean_assistance(assistance.beneficiary, data, None)
    changed = [key for key, value in clean.items() if getattr(assistance, key) != value]
    for key in changed:
        setattr(assistance, key, clean[key])
    if changed:
        assistance.save()
        log_event(AuditAction.UPDATE, request=request, obj=assistance, fields=changed)
    return assistance


@transaction.atomic
def archive_assistance(assistance: Assistance, *, request: HttpRequest | None = None) -> None:
    """Ошибочную запись переводят в архив. Последний факт помощи выполненной заявки
    убрать нельзя: заявка осталась бы «выполненной» без помощи (п. 4.3)."""
    if assistance.is_archived:
        return
    help_request = assistance.request
    if (
        help_request is not None
        and help_request.status == S.DONE
        and not help_request.assistance.filter(is_archived=False).exclude(pk=assistance.pk).exists()
    ):
        raise ValidationError("Это единственный факт помощи по выполненной заявке.")
    assistance.archive()
    log_event(AuditAction.ARCHIVE, request=request, obj=assistance)


@transaction.atomic
def import_assistance(
    beneficiary: Beneficiary, data: dict[str, Any], *, request: HttpRequest | None = None
) -> Assistance:
    """Факт помощи из импорта истории (ФТ-А-05, п. 7.3). Те же проверки, что при
    регистрации, но случай может быть уже закрыт: переносится прошлая помощь."""
    if beneficiary.is_archived:
        raise ValidationError("Карточка подопечного в архиве.")
    clean = _clean_assistance(beneficiary, data, None)
    assistance = Assistance.objects.create(beneficiary=beneficiary, **clean)
    log_event(
        AuditAction.CREATE,
        request=request,
        obj=assistance,
        extra={"beneficiary": str(beneficiary.pk), "channel": "import"},
    )
    return assistance
