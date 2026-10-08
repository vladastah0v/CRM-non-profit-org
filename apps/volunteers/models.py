"""Модуль «Волонтёры» (раздел 3.1 ТЗ, сущности п. 4.2)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Self

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.accounts.roles import RoleCode, user_has_role
from apps.core.dates import ADULT_AGE, full_years
from apps.core.models import ArchivableQuerySet, BaseModel
from apps.core.normalize import normalize_email, normalize_name

from . import permissions

telegram_username_validator = RegexValidator(
    r"^[A-Za-z0-9_]{5,32}$",
    "Имя пользователя Telegram — от 5 до 32 латинских букв, цифр или «_», без @.",
)


class VolunteerStatus(models.TextChoices):
    """Статусы ФТ-В-02. Допустимые переходы — в services.TRANSITIONS (п. 4.3)."""

    APPLICATION = "application", "Анкета"
    INTERVIEW = "interview", "Собеседование"
    TRAINING = "training", "Обучение"
    ACTIVE = "active", "Активен"
    PAUSED = "paused", "Пауза"
    LEFT = "left", "Выбыл"


STATUS_BADGES = {
    VolunteerStatus.APPLICATION: "bg-sky-100 text-sky-800",
    VolunteerStatus.INTERVIEW: "bg-indigo-100 text-indigo-800",
    VolunteerStatus.TRAINING: "bg-violet-100 text-violet-800",
    VolunteerStatus.ACTIVE: "bg-emerald-100 text-emerald-800",
    VolunteerStatus.PAUSED: "bg-amber-100 text-amber-800",
    VolunteerStatus.LEFT: "bg-slate-200 text-slate-700",
}


class Weekday(models.IntegerChoices):
    MONDAY = 0, "Понедельник"
    TUESDAY = 1, "Вторник"
    WEDNESDAY = 2, "Среда"
    THURSDAY = 3, "Четверг"
    FRIDAY = 4, "Пятница"
    SATURDAY = 5, "Суббота"
    SUNDAY = 6, "Воскресенье"


class VolunteerQuerySet(ArchivableQuerySet):
    def visible_to(self, user: Any) -> Self:
        """Проверка прав на уровне объекта (НФТ-ИБ-08), матрица 2.2.

        Куратору выборка сразу ограничивается набором полей «О» — остальные поля
        не загружаются из базы, а не просто скрываются в шаблоне.
        """
        if permissions.can_manage(user):
            return self.all()
        if user_has_role(user, RoleCode.CURATOR):
            return self.filter(is_archived=False).only(*permissions.LIMITED_FIELDS)
        if user_has_role(user, RoleCode.VOLUNTEER):
            return self.filter(user=user, is_archived=False)
        return self.none()


class Volunteer(BaseModel):
    """Карточка волонтёра (ФТ-В-01)."""

    last_name = models.CharField("фамилия", max_length=150)
    first_name = models.CharField("имя", max_length=150)
    middle_name = models.CharField("отчество", max_length=150, blank=True)
    birth_date = models.DateField("дата рождения")
    phone = models.CharField("телефон", max_length=20, help_text="Например, +7 916 123-45-67.")
    email = models.EmailField("email", blank=True)
    telegram_username = models.CharField(
        "Telegram",
        max_length=32,
        blank=True,
        validators=[telegram_username_validator],
        help_text="Имя пользователя без @.",
    )
    # Заполняется при привязке бота одноразовым кодом из CRM (п. 7.2)
    telegram_id = models.BigIntegerField(
        "Telegram ID", null=True, blank=True, unique=True, editable=False
    )
    # Код привязки: в базе — только HMAC кода, сам код видит координатор один раз
    bot_link_code = models.CharField(max_length=64, blank=True, editable=False, db_index=True)
    bot_link_code_expires = models.DateTimeField(null=True, blank=True, editable=False)
    city = models.CharField("город", max_length=150, blank=True)
    district = models.ForeignKey(
        "core.District", verbose_name="район", on_delete=models.PROTECT, null=True, blank=True
    )
    skills = models.ManyToManyField(
        "core.Skill", verbose_name="навыки", blank=True, related_name="volunteers"
    )
    programs = models.ManyToManyField(
        "core.Program", verbose_name="программы", blank=True, related_name="volunteers"
    )
    source = models.ForeignKey(
        "core.VolunteerSource",
        verbose_name="источник прихода",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    photo = models.ForeignKey(
        "consents.Attachment",
        verbose_name="фото",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="volunteer_photos",
    )

    status = models.CharField(
        "статус",
        max_length=20,
        choices=VolunteerStatus.choices,
        default=VolunteerStatus.APPLICATION,
        editable=False,  # меняется только сервисом переходов (п. 4.3)
    )
    status_changed_at = models.DateTimeField("статус изменён", default=timezone.now, editable=False)
    start_date = models.DateField(
        "дата начала", null=True, blank=True, help_text="Первый переход в статус «Активен»."
    )
    leaving_reason = models.ForeignKey(
        "core.LeavingReason",
        verbose_name="причина выбытия",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
    )
    left_on = models.DateField("дата выбытия", null=True, blank=True, editable=False)
    is_possible_duplicate = models.BooleanField(
        "возможный дубль",
        default=False,
        help_text="Анкета с сайта совпала с существующей карточкой — проверьте вручную.",
    )

    # Законный представитель несовершеннолетнего (ФТ-В-08)
    guardian_name = models.CharField("ФИО законного представителя", max_length=300, blank=True)
    guardian_relation = models.CharField(
        "кем приходится", max_length=100, blank=True, help_text="Мать, отец, опекун…"
    )
    guardian_phone = models.CharField("телефон представителя", max_length=20, blank=True)
    guardian_email = models.EmailField("email представителя", blank=True)

    # Учётная запись волонтёра: создаётся при привязке бота, без пароля и входа в
    # веб-интерфейс — нужна, чтобы действия через бота были подписаны автором
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="учётная запись",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="volunteer_profile",
    )

    # Нормализованное ФИО: поиск и проверка дублей по триграммам (п. 4.4)
    search_name = models.CharField(max_length=500, editable=False, default="")

    history = HistoricalRecords(
        history_id_field=models.UUIDField(default=uuid.uuid4),
        excluded_fields=["search_name", "bot_link_code", "bot_link_code_expires"],
        verbose_name="история волонтёра",
    )
    objects = VolunteerQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        verbose_name = "волонтёр"
        verbose_name_plural = "волонтёры"
        ordering = ["last_name", "first_name", "middle_name"]
        indexes = [
            models.Index(fields=["status"], name="volunteer_status_idx"),
            models.Index(fields=["email"], name="volunteer_email_idx"),
            models.Index(fields=["birth_date"], name="volunteer_birth_date_idx"),
            GinIndex(
                fields=["search_name"], opclasses=["gin_trgm_ops"], name="volunteer_name_trgm"
            ),
            GinIndex(fields=["phone"], opclasses=["gin_trgm_ops"], name="volunteer_phone_trgm"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(status=VolunteerStatus.LEFT) | Q(leaving_reason__isnull=False),
                name="volunteer_left_has_reason",
                violation_error_message="Для статуса «Выбыл» нужна причина.",
            ),
        ]

    def __str__(self) -> str:
        return self.full_name

    def get_absolute_url(self) -> str:
        return reverse("volunteers:detail", args=[self.pk])

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.search_name = normalize_name(self.last_name, self.first_name, self.middle_name)
        self.email = normalize_email(self.email)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and {"last_name", "first_name", "middle_name"} & set(
            update_fields
        ):
            kwargs["update_fields"] = {*update_fields, "search_name"}
        super().save(*args, **kwargs)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.last_name, self.first_name, self.middle_name) if p)

    @property
    def age(self) -> int:
        return full_years(self.birth_date)

    @property
    def is_minor(self) -> bool:
        return self.age < ADULT_AGE

    @property
    def status_badge(self) -> str:
        return STATUS_BADGES.get(VolunteerStatus(self.status), "")


class VolunteerAvailability(BaseModel):
    """Доступность по дням недели и времени (ФТ-В-01)."""

    volunteer = models.ForeignKey(Volunteer, on_delete=models.CASCADE, related_name="availability")
    weekday = models.PositiveSmallIntegerField("день недели", choices=Weekday.choices)
    time_from = models.TimeField("с")
    time_to = models.TimeField("до")

    class Meta:
        verbose_name = "доступность"
        verbose_name_plural = "доступность"
        ordering = ["weekday", "time_from"]
        indexes = [models.Index(fields=["weekday", "time_from", "time_to"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(time_to__gt=models.F("time_from")),
                name="availability_time_order",
                violation_error_message="Время «до» должно быть позже времени «с».",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_weekday_display()} {self.time_from:%H:%M}–{self.time_to:%H:%M}"


class VolunteerTraining(BaseModel):
    """Пройденное обучение или инструктаж (ФТ-В-06)."""

    volunteer = models.ForeignKey(Volunteer, on_delete=models.PROTECT, related_name="trainings")
    training_type = models.ForeignKey(
        "core.TrainingType", verbose_name="обучение", on_delete=models.PROTECT
    )
    completed_on = models.DateField("дата прохождения")
    conducted_by = models.CharField("кто провёл", max_length=200, blank=True)

    class Meta:
        verbose_name = "обучение волонтёра"
        verbose_name_plural = "обучение волонтёров"
        ordering = ["-completed_on", "-created_at"]

    def __str__(self) -> str:
        return f"{self.training_type} — {self.completed_on:%d.%m.%Y}"


class VolunteerHours(BaseModel):
    """Часы, внесённые вручную, с комментарием (ФТ-В-07).

    Часы по сменам и визитам считаются автоматически из назначений (apps.shifts).
    """

    volunteer = models.ForeignKey(Volunteer, on_delete=models.PROTECT, related_name="manual_hours")
    worked_on = models.DateField("дата")
    hours = models.DecimalField(
        "часы",
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.25")), MaxValueValidator(Decimal(24))],
    )
    program = models.ForeignKey(
        "core.Program", verbose_name="программа", on_delete=models.PROTECT, null=True, blank=True
    )
    comment = models.CharField("комментарий", max_length=500)

    class Meta:
        verbose_name = "часы волонтёра"
        verbose_name_plural = "часы волонтёров"
        ordering = ["-worked_on", "-created_at"]
        indexes = [models.Index(fields=["worked_on"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(hours__gt=0) & Q(hours__lte=24),
                name="volunteer_hours_range",
                violation_error_message="Часы — больше 0 и не больше 24 за день.",
            ),
            models.CheckConstraint(
                condition=~Q(comment=""),
                name="volunteer_hours_comment",
                violation_error_message="Для часов, внесённых вручную, нужен комментарий.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.hours} ч — {self.worked_on:%d.%m.%Y}"


class VolunteerStatusChange(BaseModel):
    """Журнал смены статусов: история карточки и данные для воронки (ФТ-О-03)."""

    volunteer = models.ForeignKey(
        Volunteer, on_delete=models.PROTECT, related_name="status_changes"
    )
    from_status = models.CharField(
        "из статуса", max_length=20, choices=VolunteerStatus.choices, blank=True
    )
    to_status = models.CharField("в статус", max_length=20, choices=VolunteerStatus.choices)
    reason = models.ForeignKey(
        "core.LeavingReason",
        verbose_name="причина",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    comment = models.CharField("комментарий", max_length=500, blank=True)

    class Meta:
        verbose_name = "смена статуса волонтёра"
        verbose_name_plural = "смены статусов волонтёров"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["to_status", "created_at"])]

    def __str__(self) -> str:
        return f"{self.from_status or '—'} → {self.to_status}"
