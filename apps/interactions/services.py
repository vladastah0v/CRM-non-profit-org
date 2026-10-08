"""Взаимодействия (ФТ-ВЗ-01) и задачи-напоминания (ФТ-ВЗ-02)."""

from __future__ import annotations

import datetime
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.roles import RoleCode, user_has_role
from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.beneficiaries import permissions as beneficiary_permissions
from apps.beneficiaries.models import Beneficiary, CuratorAccess
from apps.core.context import get_current_user
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from apps.volunteers import permissions as volunteer_permissions
from apps.volunteers.models import Volunteer

from .models import Interaction, Task, TaskStatus

Card = Volunteer | Beneficiary

# Запись «задним числом» допустима, «из будущего» — нет (с запасом на расхождение часов)
CLOCK_SKEW = datetime.timedelta(minutes=5)


def card_kwargs(card: Card) -> dict[str, Any]:
    if isinstance(card, Volunteer):
        return {"volunteer": card, "beneficiary": None}
    return {"volunteer": None, "beneficiary": card}


def can_write(user: Any, card: Card) -> bool:
    """Записи о взаимодействиях и задачи ведут те, кто работает с карточкой:
    координатор и администратор — волонтёры, куратор своего подопечного и администратор —
    подопечные (матрица 2.2, «С» — в пределах своих карточек)."""
    if isinstance(card, Volunteer):
        return volunteer_permissions.can_manage(user)
    return beneficiary_permissions.manages(user, card)


def can_edit_interaction(user: Any, interaction: Interaction) -> bool:
    """Запись правит её автор (или администратор): это свидетельство конкретного человека."""
    if user_has_role(user, RoleCode.ADMIN):
        return True
    return interaction.created_by_id == getattr(user, "pk", None) and can_write(
        user, interaction.card
    )


def _ensure_card_active(card: Card) -> None:
    if card.is_archived:
        raise ValidationError("Карточка в архиве — сначала верните её из архива.")


# ---------------------------------------------------------------------------
# Взаимодействия
# ---------------------------------------------------------------------------
def _clean_interaction(data: dict[str, Any]) -> dict[str, Any]:
    text = (data.get("text") or "").strip()
    happened_at: datetime.datetime | None = data.get("happened_at")
    errors: dict[str, list[str]] = {}
    if not text:
        errors["text"] = ["Опишите, о чём договорились."]
    if happened_at is None:
        errors["happened_at"] = ["Укажите дату и время."]
    elif happened_at > timezone.now() + CLOCK_SKEW:
        errors["happened_at"] = ["Время не может быть в будущем."]
    if errors:
        raise ValidationError(errors)
    return {"kind": data["kind"], "happened_at": happened_at, "text": text}


@transaction.atomic
def add_interaction(
    card: Card, data: dict[str, Any], *, request: HttpRequest | None = None
) -> Interaction:
    _ensure_card_active(card)
    interaction = Interaction.objects.create(**card_kwargs(card), **_clean_interaction(data))
    log_event(
        AuditAction.CREATE,
        request=request,
        obj=interaction,
        extra={str(card._meta.model_name): str(card.pk)},
    )
    return interaction


@transaction.atomic
def update_interaction(
    interaction: Interaction, data: dict[str, Any], *, request: HttpRequest | None = None
) -> Interaction:
    _ensure_card_active(interaction.card)
    clean = _clean_interaction(data)
    changed = [key for key, value in clean.items() if getattr(interaction, key) != value]
    for key in changed:
        setattr(interaction, key, clean[key])
    if changed:
        interaction.save()
        log_event(AuditAction.UPDATE, request=request, obj=interaction, fields=changed)
    return interaction


def archive_interaction(interaction: Interaction, *, request: HttpRequest | None = None) -> None:
    if not interaction.is_archived:
        interaction.archive()
        log_event(AuditAction.ARCHIVE, request=request, obj=interaction)


# ---------------------------------------------------------------------------
# Задачи
# ---------------------------------------------------------------------------
def assignees_for(card: Card) -> QuerySet[User]:
    """Кому можно поручить задачу: тем, кто видит карточку полностью.

    Подопечный — его кураторы (ответственный и с открытым доступом) и администраторы;
    волонтёр — координаторы и администраторы. Так задача не открывает чужую карточку.
    """
    active = User.objects.filter(is_active=True)
    admins = active.filter(roles__code=RoleCode.ADMIN) | active.filter(is_superuser=True)
    if isinstance(card, Volunteer):
        people = admins | active.filter(roles__code=RoleCode.COORDINATOR)
    else:
        shared = CuratorAccess.objects.filter(beneficiary=card, is_archived=False).values("user")
        curators = active.filter(roles__code=RoleCode.CURATOR).filter(
            pk__in=[card.curator_id, *shared.values_list("user", flat=True)]
        )
        people = admins | curators
    return people.distinct().order_by("last_name", "first_name")


def _clean_task(card: Card, data: dict[str, Any], *, is_new: bool) -> dict[str, Any]:
    text = " ".join((data.get("text") or "").split())
    due_date: datetime.date | None = data.get("due_date")
    assignee: User | None = data.get("assignee")
    errors: dict[str, list[str]] = {}
    if not text:
        errors["text"] = ["Опишите задачу."]
    if due_date is None:
        errors["due_date"] = ["Укажите срок."]
    elif is_new and due_date < timezone.localdate():
        errors["due_date"] = ["Срок не может быть в прошлом."]
    if assignee is None or not assignees_for(card).filter(pk=assignee.pk).exists():
        errors["assignee"] = ["Выберите сотрудника, который работает с этой карточкой."]
    if errors:
        raise ValidationError(errors)
    return {"text": text, "due_date": due_date, "assignee": assignee}


def _notify_assignee(task: Task) -> None:
    author = get_current_user()
    if author is None or author.pk != task.assignee_id:
        notify(
            [task.assignee],
            NotificationKind.TASK_ASSIGNED,
            "Вам поставлена задача",
            body=f"Срок — {task.due_date:%d.%m.%Y}.",
            url=task.card.get_absolute_url(),
        )


@transaction.atomic
def add_task(card: Card, data: dict[str, Any], *, request: HttpRequest | None = None) -> Task:
    _ensure_card_active(card)
    task = Task.objects.create(**card_kwargs(card), **_clean_task(card, data, is_new=True))
    log_event(
        AuditAction.CREATE,
        request=request,
        obj=task,
        extra={str(card._meta.model_name): str(card.pk)},
    )
    _notify_assignee(task)
    return task


@transaction.atomic
def update_task(task: Task, data: dict[str, Any], *, request: HttpRequest | None = None) -> Task:
    if task.status != TaskStatus.OPEN:
        raise ValidationError("Изменять можно только открытую задачу.")
    clean = _clean_task(task.card, data, is_new=False)
    changed = [key for key, value in clean.items() if getattr(task, key) != value]
    for key in changed:
        setattr(task, key, clean[key])
    if "due_date" in changed or "assignee" in changed:
        task.reminded_on = None  # новый срок или исполнитель — новое напоминание
    if changed:
        task.save()
        log_event(AuditAction.UPDATE, request=request, obj=task, fields=changed)
        if "assignee" in changed:
            _notify_assignee(task)
    return task


@transaction.atomic
def close_task(task: Task, *, done: bool, request: HttpRequest | None = None) -> Task:
    if task.status != TaskStatus.OPEN:
        raise ValidationError("Задача уже закрыта.")
    task.status = TaskStatus.DONE if done else TaskStatus.CANCELLED
    task.done_at = timezone.now()
    task.save(update_fields=["status", "done_at"])
    log_event(
        AuditAction.UPDATE, request=request, obj=task, fields=["status"], extra={"to": task.status}
    )
    return task


def can_manage_task(user: Any, task: Task) -> bool:
    """Закрыть или изменить задачу: исполнитель, автор, администратор."""
    if user_has_role(user, RoleCode.ADMIN):
        return True
    pk = getattr(user, "pk", None)
    return pk is not None and pk in (task.assignee_id, task.created_by_id)


# ---------------------------------------------------------------------------
# Напоминания (Celery Beat, ежедневно)
# ---------------------------------------------------------------------------
def send_task_reminders(today: datetime.date | None = None) -> int:
    """ФТ-ВЗ-02: исполнителю — напоминание в день срока (и о просроченных — один раз)."""
    today = today or timezone.localdate()
    due = Task.objects.open().filter(due_date__lte=today, reminded_on__isnull=True)
    count = 0
    for task in due.select_related("assignee", "volunteer", "beneficiary"):
        overdue = task.due_date < today
        notify(
            [task.assignee],
            NotificationKind.TASK_DUE,
            "Просрочена задача" if overdue else "Сегодня срок задачи",
            # Текст задачи не копируется: в нём бывают ФИО — задача открывается по ссылке
            body=f"Срок — {task.due_date:%d.%m.%Y}.",
            url=task.card.get_absolute_url(),
        )
        task.reminded_on = today
        task.save(update_fields=["reminded_on"])
        count += 1
    return count
