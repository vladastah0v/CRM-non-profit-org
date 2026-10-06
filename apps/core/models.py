"""Базовая модель и справочники.

Правила п. 4.1 ТЗ: UUID-ключ, created_at/updated_at/created_by/updated_by в каждой таблице,
архивирование вместо удаления, справочники — таблицы, а не перечисления в коде.
"""

from __future__ import annotations

import uuid
from typing import Any, Self

from django.conf import settings
from django.db import models
from django.utils import timezone

from .context import get_current_user


class ArchivableQuerySet(models.QuerySet):
    def active(self) -> Self:
        return self.filter(is_archived=False)

    def archived(self) -> Self:
        return self.filter(is_archived=True)


class BaseModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField("создано", default=timezone.now, editable=False)
    updated_at = models.DateTimeField("изменено", auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="кто создал",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="кто изменил",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )
    is_archived = models.BooleanField("в архиве", default=False, db_index=True)
    archived_at = models.DateTimeField("дата архивации", null=True, blank=True, editable=False)

    objects = ArchivableQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        user = get_current_user()
        if user is not None:
            if self._state.adding and self.created_by_id is None:
                self.created_by = user
            self.updated_by = user
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "updated_by", "updated_at"}
        super().save(*args, **kwargs)

    def archive(self) -> None:
        """Перевод в архив. Физическое удаление — только обезличиванием (ФТ-А-04)."""
        self.is_archived = True
        self.archived_at = timezone.now()
        self.save(update_fields=["is_archived", "archived_at"])

    def restore(self) -> None:
        self.is_archived = False
        self.archived_at = None
        self.save(update_fields=["is_archived", "archived_at"])


# ---------------------------------------------------------------------------
# Справочники (ФТ-А-02). Правит администратор, остальные сотрудники — только чтение.
# ---------------------------------------------------------------------------
class ReferenceQuerySet(ArchivableQuerySet):
    def visible_to(self, user: Any) -> Self:
        from apps.accounts.roles import STAFF_ROLES, user_has_role

        if user_has_role(user, *STAFF_ROLES):
            return self.all()
        return self.none()


class ReferenceModel(BaseModel):
    name = models.CharField("название", max_length=200, unique=True)
    description = models.TextField("описание", blank=True)
    sort_order = models.PositiveSmallIntegerField("порядок", default=100)

    objects = ReferenceQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        abstract = True
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name


class Skill(ReferenceModel):
    class Meta(ReferenceModel.Meta):
        verbose_name = "навык"
        verbose_name_plural = "навыки"


class Program(ReferenceModel):
    """Направление работы или грантовый проект фонда."""

    grantor = models.CharField("грантодатель", max_length=300, blank=True)
    contract_number = models.CharField("номер договора", max_length=100, blank=True)
    start_date = models.DateField("дата начала", null=True, blank=True)
    end_date = models.DateField("дата окончания", null=True, blank=True)

    class Meta(ReferenceModel.Meta):
        verbose_name = "программа"
        verbose_name_plural = "программы"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True)
                | models.Q(start_date__isnull=True)
                | models.Q(end_date__gte=models.F("start_date")),
                name="program_end_after_start",
                violation_error_message="Дата окончания программы раньше даты начала.",
            )
        ]


class AssistanceType(ReferenceModel):
    unit = models.CharField("единица измерения", max_length=50, blank=True)
    category = models.CharField("категория", max_length=200, blank=True)

    class Meta(ReferenceModel.Meta):
        verbose_name = "тип помощи"
        verbose_name_plural = "типы помощи"


class BeneficiaryCategory(ReferenceModel):
    class Meta(ReferenceModel.Meta):
        verbose_name = "категория подопечных"
        verbose_name_plural = "категории подопечных"


class LeavingReason(ReferenceModel):
    """Причина выбытия волонтёра (ФТ-В-02)."""

    class Meta(ReferenceModel.Meta):
        verbose_name = "причина выбытия"
        verbose_name_plural = "причины выбытия"


class District(ReferenceModel):
    name = models.CharField("название", max_length=200)
    city = models.CharField("город", max_length=200, blank=True)

    class Meta(ReferenceModel.Meta):
        verbose_name = "район"
        verbose_name_plural = "районы"
        constraints = [
            models.UniqueConstraint(
                fields=["city", "name"],
                name="district_unique_in_city",
                violation_error_message="Такой район в этом городе уже есть.",
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.city})" if self.city else self.name


class TrainingType(ReferenceModel):
    """Обучение и инструктажи волонтёров (ФТ-В-06)."""

    class Meta(ReferenceModel.Meta):
        verbose_name = "тип обучения"
        verbose_name_plural = "типы обучения"


class VolunteerSource(ReferenceModel):
    """Источник прихода волонтёра (ФТ-В-01): сайт, знакомые, мероприятие и т. п."""

    class Meta(ReferenceModel.Meta):
        verbose_name = "источник прихода волонтёра"
        verbose_name_plural = "источники прихода волонтёров"


class BeneficiarySource(ReferenceModel):
    """Источник обращения подопечного (ФТ-П-01): сайт, соцзащита, больница и т. п."""

    class Meta(ReferenceModel.Meta):
        verbose_name = "источник обращения"
        verbose_name_plural = "источники обращения"


class CaseClosingReason(ReferenceModel):
    """Причина закрытия случая подопечного (ФТ-П-04)."""

    class Meta(ReferenceModel.Meta):
        verbose_name = "причина закрытия случая"
        verbose_name_plural = "причины закрытия случая"


# ---------------------------------------------------------------------------
# Сохранённые фильтры списков (п. 3.10): не теряются после выхода из системы
# ---------------------------------------------------------------------------
class SavedListFilter(BaseModel):
    """Последние фильтры и сортировка списка у пользователя.

    Хранятся только структурные фильтры (статус, навыки, район…). Текст поиска не
    сохраняется: в нём бывают ФИО и телефоны, а это лишняя копия персональных данных.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_list_filters"
    )
    list_key = models.CharField("список", max_length=50)
    query = models.CharField("параметры", max_length=2000, blank=True)

    class Meta:
        verbose_name = "сохранённый фильтр"
        verbose_name_plural = "сохранённые фильтры"
        constraints = [
            models.UniqueConstraint(fields=["user", "list_key"], name="saved_filter_unique"),
        ]

    def __str__(self) -> str:
        return self.list_key
