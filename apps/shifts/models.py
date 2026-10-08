"""Модуль «Смены и мероприятия» (раздел 3.4 ТЗ, сущности п. 4.2)."""

from __future__ import annotations

import datetime
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Self

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q, QuerySet
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.accounts.roles import RoleCode, user_has_role
from apps.beneficiaries.models import Beneficiary
from apps.core.models import ArchivableQuerySet, BaseModel
from apps.volunteers.models import Volunteer

from . import permissions

MAX_HOURS = Decimal(24)
HOURS_STEP = Decimal("0.25")


def hours_between(start: datetime.datetime, end: datetime.datetime) -> Decimal:
    """Длительность в часах с шагом 15 минут, не больше 24 — часы по умолчанию для явки."""
    raw = Decimal((end - start).total_seconds()) / Decimal(3600)
    steps = (raw / HOURS_STEP).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return min(max(steps * HOURS_STEP, HOURS_STEP), MAX_HOURS)


class ShiftStatus(models.TextChoices):
    PLANNED = "planned", "Запланирована"
    CANCELLED = "cancelled", "Отменена"


class ShiftQuerySet(ArchivableQuerySet):
    def visible_to(self, user: Any) -> Self:
        """Матрица 2.2, «Смены и назначения»: администратор и координатор — П, куратор — Ч.

        Волонтёру (через бота) видны неархивные смены: из них он выбирает, куда записаться.
        """
        if permissions.can_manage(user):
            return self.all()
        if user_has_role(user, RoleCode.CURATOR, RoleCode.VOLUNTEER):
            return self.filter(is_archived=False)
        return self.none()

    def planned(self) -> Self:
        return self.filter(status=ShiftStatus.PLANNED, is_archived=False)


class Shift(BaseModel):
    """Смена или мероприятие (ФТ-С-01)."""

    title = models.CharField("название", max_length=200)
    shift_type = models.ForeignKey("core.ShiftType", verbose_name="тип", on_delete=models.PROTECT)
    starts_at = models.DateTimeField("начало")
    ends_at = models.DateTimeField("окончание")
    address = models.CharField("место", max_length=500)
    district = models.ForeignKey(
        "core.District", verbose_name="район", on_delete=models.PROTECT, null=True, blank=True
    )
    volunteers_needed = models.PositiveSmallIntegerField(
        "нужно волонтёров", validators=[MinValueValidator(1), MaxValueValidator(500)]
    )
    skills = models.ManyToManyField(
        "core.Skill", verbose_name="требуемые навыки", blank=True, related_name="shifts"
    )
    program = models.ForeignKey(
        "core.Program", verbose_name="программа", on_delete=models.PROTECT, null=True, blank=True
    )
    description = models.TextField(
        "что делать", blank=True, help_text="Видят волонтёры в боте: задачи, что взять с собой."
    )
    allow_signup = models.BooleanField(
        "запись через бота",
        default=True,
        help_text="Волонтёры с нужными навыками увидят смену в боте и смогут записаться.",
    )
    status = models.CharField(
        "статус",
        max_length=20,
        choices=ShiftStatus.choices,
        default=ShiftStatus.PLANNED,
        editable=False,
    )
    cancel_reason = models.CharField("причина отмены", max_length=500, blank=True, editable=False)

    history = HistoricalRecords(
        history_id_field=models.UUIDField(default=uuid.uuid4), verbose_name="история смены"
    )
    objects = ShiftQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        verbose_name = "смена"
        verbose_name_plural = "смены"
        ordering = ["starts_at"]
        indexes = [
            models.Index(fields=["status", "starts_at"], name="shift_status_start_idx"),
            models.Index(fields=["program", "starts_at"], name="shift_program_idx"),
        ]
        constraints = [
            # п. 4.4: окончание смены позже начала
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")),
                name="shift_ends_after_start",
                violation_error_message="Окончание смены должно быть позже начала.",
            ),
            models.CheckConstraint(
                condition=Q(volunteers_needed__gte=1),
                name="shift_needs_volunteers",
                violation_error_message="Нужен хотя бы один волонтёр.",
            ),
            models.CheckConstraint(
                condition=~Q(status=ShiftStatus.CANCELLED) | ~Q(cancel_reason=""),
                name="shift_cancel_has_reason",
                violation_error_message="Для отмены смены нужна причина.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} — {timezone.localtime(self.starts_at):%d.%m.%Y %H:%M}"

    def get_absolute_url(self) -> str:
        return reverse("shifts:detail", args=[self.pk])

    @property
    def full_name(self) -> str:
        """Подпись для общих страниц действий."""
        return f"Смена: {self.title}, {timezone.localtime(self.starts_at):%d.%m.%Y}"

    @property
    def is_cancelled(self) -> bool:
        return self.status == ShiftStatus.CANCELLED

    @property
    def has_started(self) -> bool:
        return self.starts_at <= timezone.now()

    @property
    def has_ended(self) -> bool:
        return self.ends_at <= timezone.now()

    @property
    def default_hours(self) -> Decimal:
        return hours_between(self.starts_at, self.ends_at)

    @property
    def status_badge(self) -> str:
        if self.is_cancelled:
            return "bg-slate-200 text-slate-700"
        return (
            "bg-slate-100 text-slate-700" if self.has_ended else "bg-emerald-100 text-emerald-800"
        )

    @property
    def state_display(self) -> str:
        """Статус для списков: у прошедшей смены — «Прошла»."""
        if self.is_cancelled:
            return ShiftStatus.CANCELLED.label
        return "Прошла" if self.has_ended else ShiftStatus.PLANNED.label


class AssignmentKind(models.TextChoices):
    SHIFT = "shift", "На смену"
    VISIT = "visit", "Визит к подопечному"


class AssignmentStatus(models.TextChoices):
    """Статусы назначения (п. 4.3): назначен → подтвердил → пришёл / не пришёл."""

    ASSIGNED = "assigned", "Назначен"
    CONFIRMED = "confirmed", "Подтвердил"
    ATTENDED = "attended", "Пришёл"
    NO_SHOW = "no_show", "Не пришёл"
    DECLINED = "declined", "Отказался"
    CANCELLED = "cancelled", "Снят"


A = AssignmentStatus
# Ждут смены или визита
PENDING_STATUSES = (A.ASSIGNED, A.CONFIRMED)
# Занимают место на смене и время волонтёра
OCCUPYING_STATUSES = (A.ASSIGNED, A.CONFIRMED, A.ATTENDED)

ASSIGNMENT_BADGES = {
    A.ASSIGNED: "bg-sky-100 text-sky-800",
    A.CONFIRMED: "bg-indigo-100 text-indigo-800",
    A.ATTENDED: "bg-emerald-100 text-emerald-800",
    A.NO_SHOW: "bg-red-100 text-red-800",
    A.DECLINED: "bg-slate-200 text-slate-700",
    A.CANCELLED: "bg-slate-200 text-slate-700",
}


class AssignmentQuerySet(ArchivableQuerySet):
    def visible_to(self, user: Any) -> Self:
        """Матрица 2.2: администратор и координатор — всё; куратор — назначения на смены
        и визиты к своим подопечным; волонтёр — только свои."""
        if permissions.can_manage(user):
            return self.all()
        condition = Q(pk__in=[])
        if user_has_role(user, RoleCode.CURATOR):
            condition |= Q(is_archived=False) & (
                Q(kind=AssignmentKind.SHIFT) | Q(beneficiary__in=Beneficiary.objects.owned_by(user))
            )
        if user_has_role(user, RoleCode.VOLUNTEER):
            condition |= Q(is_archived=False, volunteer__user=user)
        return self.filter(condition)

    def with_times(self) -> QuerySet[Any]:
        """Начало и конец (starts, ends) — смены или визита: для сортировки, пересечений
        и напоминаний. Тип — QuerySet[Any]: аннотации не видны проверке типов."""
        return self.annotate(
            starts=Coalesce("shift__starts_at", "visit_starts_at"),
            ends=Coalesce("shift__ends_at", "visit_ends_at"),
        )

    def active(self) -> Self:
        """Не в архиве и не на отменённой смене."""
        return self.filter(is_archived=False).exclude(shift__status=ShiftStatus.CANCELLED)


class Assignment(BaseModel):
    """Назначение волонтёра на смену или визит к подопечному (ФТ-С-02, ФТ-С-05).

    Визит к подопечному — одно посещение с датой и временем; регулярные визиты —
    серия таких назначений. Часы вносятся только для статуса «Пришёл» (п. 4.3).
    """

    kind = models.CharField("тип", max_length=10, choices=AssignmentKind.choices)
    volunteer = models.ForeignKey(
        Volunteer, verbose_name="волонтёр", on_delete=models.PROTECT, related_name="assignments"
    )
    shift = models.ForeignKey(
        Shift,
        verbose_name="смена",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assignments",
    )
    beneficiary = models.ForeignKey(
        Beneficiary,
        verbose_name="подопечный",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="visits",
    )
    visit_starts_at = models.DateTimeField("начало визита", null=True, blank=True)
    visit_ends_at = models.DateTimeField("окончание визита", null=True, blank=True)
    task_comment = models.TextField(
        "комментарий к заданию",
        blank=True,
        help_text="Видит волонтёр в боте. Без сведений о здоровье и документах.",
    )
    program = models.ForeignKey(
        "core.Program", verbose_name="программа", on_delete=models.PROTECT, null=True, blank=True
    )
    status = models.CharField(
        "статус",
        max_length=20,
        choices=AssignmentStatus.choices,
        default=AssignmentStatus.ASSIGNED,
        editable=False,  # меняется только сервисом переходов (п. 4.3)
    )
    status_changed_at = models.DateTimeField("статус изменён", default=timezone.now, editable=False)
    hours = models.DecimalField(
        "часы",
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(HOURS_STEP), MaxValueValidator(MAX_HOURS)],
    )
    # Отметки отправленных сообщений в Telegram (ФТ-У-01) — чтобы не повторять
    reminded_day_at = models.DateTimeField(null=True, blank=True, editable=False)
    reminded_soon_at = models.DateTimeField(null=True, blank=True, editable=False)
    attendance_asked_at = models.DateTimeField(null=True, blank=True, editable=False)

    history = HistoricalRecords(
        history_id_field=models.UUIDField(default=uuid.uuid4),
        excluded_fields=["reminded_day_at", "reminded_soon_at", "attendance_asked_at"],
        verbose_name="история назначения",
    )
    objects = AssignmentQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        verbose_name = "назначение"
        verbose_name_plural = "назначения"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["volunteer", "status"], name="assignment_volunteer_idx"),
            models.Index(fields=["status", "visit_starts_at"], name="assignment_visit_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        kind=AssignmentKind.SHIFT,
                        shift__isnull=False,
                        beneficiary__isnull=True,
                        visit_starts_at__isnull=True,
                        visit_ends_at__isnull=True,
                    )
                    | Q(
                        kind=AssignmentKind.VISIT,
                        shift__isnull=True,
                        beneficiary__isnull=False,
                        visit_starts_at__isnull=False,
                        visit_ends_at__isnull=False,
                    )
                ),
                name="assignment_kind_target",
                violation_error_message="Назначение — либо на смену, либо визит к подопечному.",
            ),
            models.CheckConstraint(
                condition=Q(visit_ends_at__isnull=True) | Q(visit_ends_at__gt=F("visit_starts_at")),
                name="assignment_visit_ends_after_start",
                violation_error_message="Окончание визита должно быть позже начала.",
            ),
            models.CheckConstraint(
                condition=Q(hours__isnull=True) | Q(status=AssignmentStatus.ATTENDED),
                name="assignment_hours_only_attended",
                violation_error_message="Часы вносятся только для статуса «Пришёл».",
            ),
            models.CheckConstraint(
                condition=Q(hours__isnull=True) | (Q(hours__gt=0) & Q(hours__lte=MAX_HOURS)),
                name="assignment_hours_range",
                violation_error_message="Часы — больше 0 и не больше 24.",
            ),
            models.UniqueConstraint(
                fields=["shift", "volunteer"],
                condition=Q(shift__isnull=False),
                name="assignment_unique_volunteer_on_shift",
                violation_error_message="Волонтёр уже есть в списке этой смены.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} — {timezone.localtime(self.starts_at):%d.%m.%Y %H:%M}"

    def get_absolute_url(self) -> str:
        if self.shift_id is not None:
            return reverse("shifts:detail", args=[self.shift_id])
        return reverse("shifts:visit_detail", args=[self.pk])

    @property
    def is_visit(self) -> bool:
        return self.kind == AssignmentKind.VISIT

    @property
    def starts_at(self) -> datetime.datetime:
        value = self.shift.starts_at if self.shift is not None else self.visit_starts_at
        if value is None:  # исключено ограничением assignment_kind_target
            raise ValueError("Назначение без времени начала")
        return value

    @property
    def ends_at(self) -> datetime.datetime:
        value = self.shift.ends_at if self.shift is not None else self.visit_ends_at
        if value is None:
            raise ValueError("Назначение без времени окончания")
        return value

    @property
    def title(self) -> str:
        return self.shift.title if self.shift is not None else "Визит к подопечному"

    @property
    def full_name(self) -> str:
        """Подпись для общих страниц действий."""
        return f"{self.volunteer.full_name} — {self.title.lower() if self.is_visit else self.title}"

    @property
    def is_pending(self) -> bool:
        return self.status in PENDING_STATUSES

    @property
    def has_started(self) -> bool:
        return self.starts_at <= timezone.now()

    @property
    def default_hours(self) -> Decimal:
        return hours_between(self.starts_at, self.ends_at)

    @property
    def effective_program(self) -> Any:
        return self.shift.program if self.shift is not None else self.program

    @property
    def status_badge(self) -> str:
        return ASSIGNMENT_BADGES.get(AssignmentStatus(self.status), "")


class AssignmentStatusChange(BaseModel):
    """Журнал статусов назначения: история и статистика явки волонтёра (ФТ-С-04)."""

    assignment = models.ForeignKey(
        Assignment, on_delete=models.PROTECT, related_name="status_changes"
    )
    from_status = models.CharField(
        "из статуса", max_length=20, choices=AssignmentStatus.choices, blank=True
    )
    to_status = models.CharField("в статус", max_length=20, choices=AssignmentStatus.choices)
    via_bot = models.BooleanField("через бота", default=False)
    comment = models.CharField("комментарий", max_length=500, blank=True)

    class Meta:
        verbose_name = "смена статуса назначения"
        verbose_name_plural = "смены статусов назначений"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.from_status or '—'} → {self.to_status}"
