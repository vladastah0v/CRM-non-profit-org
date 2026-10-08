"""Модуль «Взаимодействия» (раздел 3.5 ТЗ): записи о звонках и встречах, задачи-напоминания.

Связь с карточкой — двумя внешними ключами (волонтёр или подопечный), заполнен ровно
один (п. 4.2 ТЗ).
"""

from __future__ import annotations

from typing import Any, Self

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.accounts.roles import RoleCode, user_has_role
from apps.beneficiaries.models import Beneficiary
from apps.core.models import ArchivableQuerySet, BaseModel

ONE_CARD = Q(volunteer__isnull=False, beneficiary__isnull=True) | Q(
    volunteer__isnull=True, beneficiary__isnull=False
)


def _cards_of(user: Any) -> Q:
    """Карточки, с которыми сотрудник работает по матрице 2.2 («С» — в пределах своих):
    куратор — свои подопечные, координатор — волонтёры."""
    condition = Q(pk__in=[])
    if user_has_role(user, RoleCode.CURATOR):
        condition |= Q(beneficiary__in=Beneficiary.objects.owned_by(user))
    if user_has_role(user, RoleCode.COORDINATOR):
        condition |= Q(volunteer__isnull=False)
    return condition


class CardLinkedModel(BaseModel):
    volunteer = models.ForeignKey(
        "volunteers.Volunteer",
        verbose_name="волонтёр",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="%(class)ss",
    )
    beneficiary = models.ForeignKey(
        Beneficiary,
        verbose_name="подопечный",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="%(class)ss",
    )

    class Meta:
        abstract = True

    @property
    def card(self) -> Any:
        return self.volunteer or self.beneficiary


class InteractionKind(models.TextChoices):
    CALL = "call", "Звонок"
    MEETING = "meeting", "Встреча"
    VISIT = "visit", "Визит"
    MESSAGE = "message", "Сообщение"


class InteractionQuerySet(ArchivableQuerySet):
    def visible_to(self, user: Any) -> Self:
        """Матрица 2.2, «Взаимодействия и заметки»: администратор П, координатор и куратор С."""
        if user_has_role(user, RoleCode.ADMIN):
            return self.all()
        return self.filter(_cards_of(user))


class Interaction(CardLinkedModel):
    """Звонок, встреча, визит или сообщение (ФТ-ВЗ-01). Автор — created_by."""

    kind = models.CharField("тип", max_length=20, choices=InteractionKind.choices)
    happened_at = models.DateTimeField("когда", default=timezone.now)
    text = models.TextField("содержание")

    objects = InteractionQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        verbose_name = "взаимодействие"
        verbose_name_plural = "взаимодействия"
        ordering = ["-happened_at"]
        indexes = [models.Index(fields=["beneficiary", "-happened_at"])]
        constraints = [
            models.CheckConstraint(
                condition=ONE_CARD,
                name="interaction_one_card",
                violation_error_message="Запись относится ровно к одной карточке.",
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.happened_at:%d.%m.%Y}"


class TaskStatus(models.TextChoices):
    OPEN = "open", "Открыта"
    DONE = "done", "Выполнена"
    CANCELLED = "cancelled", "Отменена"


class TaskQuerySet(ArchivableQuerySet):
    def visible_to(self, user: Any) -> Self:
        """Задачи своих карточек, а также назначенные пользователю и поставленные им."""
        if user_has_role(user, RoleCode.ADMIN):
            return self.all()
        if not getattr(user, "is_authenticated", False):
            return self.none()
        return self.filter(_cards_of(user) | Q(assignee=user) | Q(created_by=user))

    def open(self) -> Self:
        return self.filter(status=TaskStatus.OPEN, is_archived=False)


class Task(CardLinkedModel):
    """Задача-напоминание с датой и ответственным, привязанная к карточке (ФТ-ВЗ-02)."""

    text = models.CharField("что сделать", max_length=500)
    due_date = models.DateField("срок")
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="ответственный",
        on_delete=models.PROTECT,
        related_name="tasks",
    )
    status = models.CharField(
        "статус", max_length=20, choices=TaskStatus.choices, default=TaskStatus.OPEN
    )
    done_at = models.DateTimeField("выполнена", null=True, blank=True, editable=False)
    reminded_on = models.DateField(null=True, blank=True, editable=False)

    objects = TaskQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        verbose_name = "задача"
        verbose_name_plural = "задачи"
        ordering = ["due_date", "created_at"]
        indexes = [models.Index(fields=["assignee", "status", "due_date"])]
        constraints = [
            models.CheckConstraint(
                condition=ONE_CARD,
                name="task_one_card",
                violation_error_message="Задача относится ровно к одной карточке.",
            )
        ]

    def __str__(self) -> str:
        return self.text

    @property
    def is_overdue(self) -> bool:
        return self.status == TaskStatus.OPEN and self.due_date < timezone.localdate()
