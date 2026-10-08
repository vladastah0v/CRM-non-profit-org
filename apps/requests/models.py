"""Модуль «Заявки и помощь» (раздел 3.3 ТЗ, сущности п. 4.2)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Self

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.accounts.roles import RoleCode, user_has_role
from apps.beneficiaries.models import Beneficiary
from apps.core.models import ArchivableQuerySet, BaseModel


class RequestStatus(models.TextChoices):
    """Статусы ФТ-З-02. Допустимые переходы — в services.TRANSITIONS (п. 4.3)."""

    NEW = "new", "Новая"
    IN_WORK = "in_work", "В работе"
    DONE = "done", "Выполнена"
    REJECTED = "rejected", "Отклонена"
    CANCELLED = "cancelled", "Отменена"


OPEN_STATUSES = (RequestStatus.NEW, RequestStatus.IN_WORK)

STATUS_BADGES = {
    RequestStatus.NEW: "bg-sky-100 text-sky-800",
    RequestStatus.IN_WORK: "bg-indigo-100 text-indigo-800",
    RequestStatus.DONE: "bg-emerald-100 text-emerald-800",
    RequestStatus.REJECTED: "bg-slate-200 text-slate-700",
    RequestStatus.CANCELLED: "bg-slate-200 text-slate-700",
}


class Urgency(models.TextChoices):
    NORMAL = "normal", "Обычная"
    URGENT = "urgent", "Срочная"
    EMERGENCY = "emergency", "Экстренная"


URGENCY_BADGES = {
    Urgency.NORMAL: "",
    Urgency.URGENT: "bg-amber-100 text-amber-800",
    Urgency.EMERGENCY: "bg-red-100 text-red-800",
}


def _own_or_responsible(user: Any) -> Q:
    """Куратор: заявки своих подопечных и заявки, где он ответственный."""
    return Q(beneficiary__in=Beneficiary.objects.owned_by(user)) | Q(responsible=user)


class HelpRequestQuerySet(ArchivableQuerySet):
    def visible_to(self, user: Any) -> Self:
        """Матрица 2.2, «Заявки»: администратор П, координатор Ч, куратор С."""
        if user_has_role(user, RoleCode.ADMIN):
            return self.all()
        condition = Q(pk__in=[])
        if user_has_role(user, RoleCode.CURATOR):
            condition |= _own_or_responsible(user)
        if user_has_role(user, RoleCode.COORDINATOR):
            condition |= Q(is_archived=False)
        return self.filter(condition)

    def open(self) -> Self:
        return self.filter(status__in=OPEN_STATUSES, is_archived=False)

    def overdue(self) -> Self:
        return self.open().filter(due_date__lt=timezone.localdate())


class HelpRequest(BaseModel):
    """Заявка — зафиксированная потребность подопечного в помощи (ФТ-З-01)."""

    beneficiary = models.ForeignKey(
        Beneficiary, verbose_name="подопечный", on_delete=models.PROTECT, related_name="requests"
    )
    assistance_type = models.ForeignKey(
        "core.AssistanceType", verbose_name="тип помощи", on_delete=models.PROTECT
    )
    description = models.TextField("описание")
    urgency = models.CharField(
        "срочность", max_length=20, choices=Urgency.choices, default=Urgency.NORMAL
    )
    due_date = models.DateField("желаемая дата", null=True, blank=True)
    responsible = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="ответственный",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="responsible_requests",
    )
    status = models.CharField(
        "статус",
        max_length=20,
        choices=RequestStatus.choices,
        default=RequestStatus.NEW,
        editable=False,  # меняется только сервисом переходов (п. 4.3)
    )
    status_changed_at = models.DateTimeField("статус изменён", default=timezone.now, editable=False)
    resolution_reason = models.CharField(
        "причина отклонения или отмены", max_length=500, blank=True, editable=False
    )
    completed_at = models.DateTimeField("выполнена", null=True, blank=True, editable=False)
    # Напоминание о сроке отправлено (ФТ-У-03) — чтобы не повторять каждый день
    deadline_notified_on = models.DateField(null=True, blank=True, editable=False)

    history = HistoricalRecords(
        history_id_field=models.UUIDField(default=uuid.uuid4), verbose_name="история заявки"
    )
    objects = HelpRequestQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        verbose_name = "заявка"
        verbose_name_plural = "заявки"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "due_date"], name="request_status_due_idx"),
            models.Index(fields=["responsible", "status"], name="request_responsible_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(status__in=[RequestStatus.REJECTED, RequestStatus.CANCELLED])
                | ~Q(resolution_reason=""),
                name="request_rejection_has_reason",
                violation_error_message="Для отклонения или отмены нужна причина.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.assistance_type} — {self.beneficiary}"

    def get_absolute_url(self) -> str:
        return reverse("requests:detail", args=[self.pk])

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_overdue(self) -> bool:
        return (
            self.is_open
            and not self.is_archived
            and self.due_date is not None
            and self.due_date < timezone.localdate()
        )

    @property
    def status_badge(self) -> str:
        return STATUS_BADGES.get(RequestStatus(self.status), "")

    @property
    def urgency_badge(self) -> str:
        return URGENCY_BADGES.get(Urgency(self.urgency), "")

    @property
    def full_name(self) -> str:
        """Подпись для общих страниц действий: «Заявка: продукты»."""
        return f"Заявка: {self.assistance_type}"


class RequestStatusChange(BaseModel):
    """Журнал статусов заявки: лента истории и отчёт о времени выполнения (ФТ-О-05)."""

    request = models.ForeignKey(
        HelpRequest, on_delete=models.PROTECT, related_name="status_changes"
    )
    from_status = models.CharField(
        "из статуса", max_length=20, choices=RequestStatus.choices, blank=True
    )
    to_status = models.CharField("в статус", max_length=20, choices=RequestStatus.choices)
    comment = models.CharField("комментарий", max_length=500, blank=True)

    class Meta:
        verbose_name = "смена статуса заявки"
        verbose_name_plural = "смены статусов заявок"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.from_status or '—'} → {self.to_status}"


class AssistanceQuerySet(ArchivableQuerySet):
    def visible_to(self, user: Any) -> Self:
        """Матрица 2.2, «Оказанная помощь»: администратор П, координатор Ч, куратор С.

        Аналитику — только агрегаты, через отчёты (apps.reports), не сами записи.
        """
        if user_has_role(user, RoleCode.ADMIN):
            return self.all()
        condition = Q(pk__in=[])
        if user_has_role(user, RoleCode.CURATOR):
            condition |= Q(beneficiary__in=Beneficiary.objects.owned_by(user))
        if user_has_role(user, RoleCode.COORDINATOR):
            condition |= Q(is_archived=False)
        return self.filter(condition)


class Assistance(BaseModel):
    """Факт оказания помощи: что, кому, когда, кем, по какой программе (ФТ-З-03)."""

    beneficiary = models.ForeignKey(
        Beneficiary, verbose_name="подопечный", on_delete=models.PROTECT, related_name="assistance"
    )
    request = models.ForeignKey(
        HelpRequest,
        verbose_name="заявка",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assistance",
    )
    assistance_type = models.ForeignKey(
        "core.AssistanceType", verbose_name="тип помощи", on_delete=models.PROTECT
    )
    provided_on = models.DateField("дата")
    quantity = models.DecimalField(
        "количество",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal(0))],
    )
    unit = models.CharField("единица", max_length=50, blank=True)
    amount = models.DecimalField(
        "сумма, ₽",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal(0))],
    )
    program = models.ForeignKey(
        "core.Program", verbose_name="программа", on_delete=models.PROTECT, null=True, blank=True
    )
    executor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="исполнитель-сотрудник",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="provided_assistance",
    )
    executor_volunteer = models.ForeignKey(
        "volunteers.Volunteer",
        verbose_name="исполнитель-волонтёр",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="provided_assistance",
    )
    comment = models.TextField("комментарий", blank=True)

    history = HistoricalRecords(
        history_id_field=models.UUIDField(default=uuid.uuid4), verbose_name="история помощи"
    )
    objects = AssistanceQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        verbose_name = "оказанная помощь"
        verbose_name_plural = "оказанная помощь"
        ordering = ["-provided_on", "-created_at"]
        indexes = [
            models.Index(fields=["provided_on"], name="assistance_date_idx"),
            models.Index(fields=["program", "provided_on"], name="assistance_program_idx"),
        ]
        constraints = [
            # п. 4.4: количество и сумма неотрицательны
            models.CheckConstraint(
                condition=Q(quantity__isnull=True) | Q(quantity__gte=0),
                name="assistance_quantity_non_negative",
                violation_error_message="Количество не может быть отрицательным.",
            ),
            models.CheckConstraint(
                condition=Q(amount__isnull=True) | Q(amount__gte=0),
                name="assistance_amount_non_negative",
                violation_error_message="Сумма не может быть отрицательной.",
            ),
            models.CheckConstraint(
                condition=Q(quantity__isnull=False) | Q(amount__isnull=False),
                name="assistance_has_quantity_or_amount",
                violation_error_message="Укажите количество или сумму.",
            ),
            models.CheckConstraint(
                condition=Q(executor_user__isnull=False, executor_volunteer__isnull=True)
                | Q(executor_user__isnull=True, executor_volunteer__isnull=False),
                name="assistance_one_executor",
                violation_error_message="Укажите одного исполнителя: сотрудника или волонтёра.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.assistance_type} — {self.provided_on:%d.%m.%Y}"

    @property
    def executor(self) -> Any:
        return self.executor_user or self.executor_volunteer
