"""Модуль «Подопечные» (раздел 3.2 ТЗ, сущности п. 4.2)."""

from __future__ import annotations

import uuid
from typing import Any, Self

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.accounts.roles import RoleCode, user_has_role
from apps.core.dates import ADULT_AGE, full_years
from apps.core.fields import EncryptedTextField
from apps.core.models import ArchivableQuerySet, BaseModel
from apps.core.normalize import normalize_name

from . import permissions


class CaseStatus(models.TextChoices):
    """Статусы случая ФТ-П-04. Допустимые переходы — в services.TRANSITIONS (п. 4.3)."""

    NEW = "new", "Новый"
    ASSESSMENT = "assessment", "Оценка"
    IN_WORK = "in_work", "В работе"
    PAUSED = "paused", "На паузе"
    CLOSED = "closed", "Закрыт"


STATUS_BADGES = {
    CaseStatus.NEW: "bg-sky-100 text-sky-800",
    CaseStatus.ASSESSMENT: "bg-indigo-100 text-indigo-800",
    CaseStatus.IN_WORK: "bg-emerald-100 text-emerald-800",
    CaseStatus.PAUSED: "bg-amber-100 text-amber-800",
    CaseStatus.CLOSED: "bg-slate-200 text-slate-700",
}


class BeneficiaryQuerySet(ArchivableQuerySet):
    def owned_by(self, user: Any) -> Self:
        """«Свои» подопечные куратора: ответственный или открыт доступ к случаю (п. 2.3)."""
        if not getattr(user, "is_authenticated", False):
            return self.none()
        shared = CuratorAccess.objects.filter(user=user, is_archived=False).values("beneficiary")
        return self.filter(Q(curator=user) | Q(pk__in=shared))

    def visible_to(self, user: Any) -> Self:
        """Проверка прав на уровне объекта (НФТ-ИБ-08), матрица 2.2.

        Координатору (без ролей с полным доступом) выборка сразу ограничивается набором
        полей «О» — остальные поля не загружаются из базы.
        """
        if user_has_role(user, RoleCode.ADMIN):
            return self.all()
        is_curator = user_has_role(user, RoleCode.CURATOR)
        is_coordinator = user_has_role(user, RoleCode.COORDINATOR)
        if is_curator and is_coordinator:
            return self.filter(Q(pk__in=self.owned_by(user).values("pk")) | Q(is_archived=False))
        if is_curator:
            return self.owned_by(user)
        if is_coordinator:
            return self.filter(is_archived=False).only(*permissions.LIMITED_FIELDS)
        return self.none()


class Beneficiary(BaseModel):
    """Карточка подопечного (ФТ-П-01). Человек или глава семьи; члены семьи и
    представители — связанные лица (ФТ-П-02)."""

    last_name = models.CharField("фамилия", max_length=150)
    first_name = models.CharField("имя", max_length=150)
    middle_name = models.CharField("отчество", max_length=150, blank=True)
    birth_date = models.DateField("дата рождения")
    address = models.CharField("адрес", max_length=500, blank=True)
    phone = models.CharField("телефон", max_length=20, blank=True)
    category = models.ForeignKey(
        "core.BeneficiaryCategory",
        verbose_name="категория",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    source = models.ForeignKey(
        "core.BeneficiarySource",
        verbose_name="источник обращения",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    curator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="ответственный куратор",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="curated_beneficiaries",
    )
    programs = models.ManyToManyField(
        "core.Program", verbose_name="программы", blank=True, related_name="beneficiaries"
    )

    status = models.CharField(
        "статус случая",
        max_length=20,
        choices=CaseStatus.choices,
        default=CaseStatus.NEW,
        editable=False,  # меняется только сервисом переходов (п. 4.3)
    )
    status_changed_at = models.DateTimeField("статус изменён", default=timezone.now, editable=False)
    closed_on = models.DateField("дата закрытия", null=True, blank=True, editable=False)
    closing_reason = models.ForeignKey(
        "core.CaseClosingReason",
        verbose_name="причина закрытия",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
    )
    outcome = models.TextField("итог", blank=True, editable=False)

    # Нормализованное ФИО: поиск и проверка дублей по триграммам (п. 4.4)
    search_name = models.CharField(max_length=500, editable=False, default="")

    history = HistoricalRecords(
        history_id_field=models.UUIDField(default=uuid.uuid4),
        excluded_fields=["search_name"],
        verbose_name="история подопечного",
    )
    objects = BeneficiaryQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        verbose_name = "подопечный"
        verbose_name_plural = "подопечные"
        ordering = ["last_name", "first_name", "middle_name"]
        indexes = [
            models.Index(fields=["status"], name="beneficiary_status_idx"),
            models.Index(fields=["birth_date"], name="beneficiary_birth_date_idx"),
            GinIndex(
                fields=["search_name"], opclasses=["gin_trgm_ops"], name="beneficiary_name_trgm"
            ),
            GinIndex(fields=["phone"], opclasses=["gin_trgm_ops"], name="beneficiary_phone_trgm"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(status=CaseStatus.CLOSED)
                | (Q(closing_reason__isnull=False) & ~Q(outcome="")),
                name="beneficiary_closed_has_reason",
                violation_error_message="Для закрытия случая нужны причина и итог.",
            ),
            models.CheckConstraint(
                condition=~Q(status=CaseStatus.IN_WORK) | Q(curator__isnull=False),
                name="beneficiary_in_work_has_curator",
                violation_error_message="Случай в работе должен вести куратор.",
            ),
        ]

    def __str__(self) -> str:
        return self.full_name

    def get_absolute_url(self) -> str:
        return reverse("beneficiaries:detail", args=[self.pk])

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.search_name = normalize_name(self.last_name, self.first_name, self.middle_name)
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
        return STATUS_BADGES.get(CaseStatus(self.status), "")


class CuratorAccess(BaseModel):
    """Доступ к случаю второго куратора, открытый руководителем (п. 2.3).

    Кто и когда открыл доступ — created_by / created_at; закрытие — архивирование.
    """

    beneficiary = models.ForeignKey(
        Beneficiary, on_delete=models.PROTECT, related_name="curator_access"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="куратор",
        on_delete=models.PROTECT,
        related_name="shared_beneficiaries",
    )

    class Meta:
        verbose_name = "доступ куратора к случаю"
        verbose_name_plural = "доступ кураторов к случаям"
        constraints = [
            models.UniqueConstraint(
                fields=["beneficiary", "user"],
                condition=Q(is_archived=False),
                name="curator_access_unique_active",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} → {self.beneficiary_id}"


class BeneficiarySensitive(BaseModel):
    """Защищённый блок (ФТ-П-03): здоровье, инвалидность, документы — всё зашифровано.

    Видят только администратор и куратор своего подопечного; каждое открытие блока
    пишется в журнал аудита (п. 2.3).
    """

    beneficiary = models.OneToOneField(
        Beneficiary, on_delete=models.PROTECT, related_name="sensitive"
    )
    health = EncryptedTextField("здоровье", blank=True)
    disability = EncryptedTextField("инвалидность", blank=True)
    passport = EncryptedTextField("паспорт", blank=True)
    snils = EncryptedTextField("СНИЛС", blank=True)

    history = HistoricalRecords(
        history_id_field=models.UUIDField(default=uuid.uuid4),
        verbose_name="история защищённых данных",
    )

    class Meta:
        verbose_name = "защищённые данные подопечного"
        verbose_name_plural = "защищённые данные подопечных"

    def __str__(self) -> str:
        return "Защищённые данные"


class DocumentKind(models.TextChoices):
    PASSPORT = "passport", "Паспорт"
    SNILS = "snils", "СНИЛС"
    DISABILITY = "disability", "Справка об инвалидности"
    MEDICAL = "medical", "Медицинский документ"
    OTHER = "other", "Другой документ"


class BeneficiaryDocument(BaseModel):
    """Скан документа подопечного — часть защищённого блока (ФТ-П-03, ФТ-Д-05)."""

    beneficiary = models.ForeignKey(Beneficiary, on_delete=models.PROTECT, related_name="documents")
    kind = models.CharField("вид", max_length=20, choices=DocumentKind.choices)
    title = models.CharField("описание", max_length=200, blank=True)
    file = models.ForeignKey(
        "consents.Attachment",
        verbose_name="файл",
        on_delete=models.PROTECT,
        related_name="beneficiary_documents",
    )

    class Meta:
        verbose_name = "документ подопечного"
        verbose_name_plural = "документы подопечных"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.get_kind_display()


class RelatedPerson(BaseModel):
    """Член семьи или законный представитель подопечного (ФТ-П-02)."""

    beneficiary = models.ForeignKey(
        Beneficiary, on_delete=models.PROTECT, related_name="related_persons"
    )
    full_name = models.CharField("ФИО", max_length=300)
    relation = models.CharField(
        "кем приходится", max_length=100, help_text="Мать, сын, супруг, опекун, соседка…"
    )
    phone = models.CharField("телефон", max_length=20, blank=True)
    email = models.EmailField("email", blank=True)
    is_legal_representative = models.BooleanField("законный представитель", default=False)

    class Meta:
        verbose_name = "связанное лицо"
        verbose_name_plural = "связанные лица"
        ordering = ["-is_legal_representative", "full_name"]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.relation})"


class CaseStatusChange(BaseModel):
    """Журнал смены статусов случая: лента истории (ФТ-П-05) и отчёты."""

    beneficiary = models.ForeignKey(
        Beneficiary, on_delete=models.PROTECT, related_name="status_changes"
    )
    from_status = models.CharField(
        "из статуса", max_length=20, choices=CaseStatus.choices, blank=True
    )
    to_status = models.CharField("в статус", max_length=20, choices=CaseStatus.choices)
    reason = models.ForeignKey(
        "core.CaseClosingReason",
        verbose_name="причина закрытия",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    comment = models.CharField("комментарий", max_length=500, blank=True)

    class Meta:
        verbose_name = "смена статуса случая"
        verbose_name_plural = "смены статусов случаев"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["to_status", "created_at"])]

    def __str__(self) -> str:
        return f"{self.from_status or '—'} → {self.to_status}"
