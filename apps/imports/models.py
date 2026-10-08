"""Импорт из Excel и CSV с предпросмотром и отчётом об ошибках (ФТ-А-05, п. 7.3).

Загруженный файл хранится зашифрованным (в нём ПДн) и удаляется, как только импорт
завершён или отменён. В записи импорта — только номера строк, названия колонок и
тексты ошибок, без значений из файла.
"""

from __future__ import annotations

from typing import Any, Self

from django.db import models
from django.urls import reverse

from apps.accounts.roles import RoleCode, user_has_role
from apps.core.models import ArchivableQuerySet, BaseModel


class ImportKind(models.TextChoices):
    VOLUNTEERS = "volunteers", "Волонтёры"
    BENEFICIARIES = "beneficiaries", "Подопечные"
    ASSISTANCE = "assistance", "История помощи"


class ImportStatus(models.TextChoices):
    CHECKING = "checking", "Проверка файла"
    READY = "ready", "Проверен — можно импортировать"
    IMPORTING = "importing", "Импорт идёт"
    DONE = "done", "Завершён"
    FAILED = "failed", "Файл не прочитан"
    CANCELLED = "cancelled", "Отменён"


IN_PROGRESS = (ImportStatus.CHECKING, ImportStatus.IMPORTING)
FINAL = (ImportStatus.DONE, ImportStatus.FAILED, ImportStatus.CANCELLED)

STATUS_BADGES = {
    ImportStatus.CHECKING: "bg-sky-100 text-sky-800",
    ImportStatus.READY: "bg-indigo-100 text-indigo-800",
    ImportStatus.IMPORTING: "bg-sky-100 text-sky-800",
    ImportStatus.DONE: "bg-emerald-100 text-emerald-800",
    ImportStatus.FAILED: "bg-red-100 text-red-800",
    ImportStatus.CANCELLED: "bg-slate-200 text-slate-700",
}


class ImportJobQuerySet(ArchivableQuerySet):
    def visible_to(self, user: Any) -> Self:
        """Импорт — функция администрирования (ФТ-А-05, раздел 3.9)."""
        return self.all() if user_has_role(user, RoleCode.ADMIN) else self.none()


class ImportJob(BaseModel):
    kind = models.CharField("что импортируется", max_length=20, choices=ImportKind.choices)
    status = models.CharField(
        "состояние",
        max_length=20,
        choices=ImportStatus.choices,
        default=ImportStatus.CHECKING,
        editable=False,
    )
    file = models.ForeignKey(
        "consents.Attachment",
        verbose_name="файл",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )
    file_format = models.CharField("формат", max_length=10, editable=False)
    total_rows = models.PositiveIntegerField("строк", default=0, editable=False)
    # Итоги проверки (предпросмотр) и импорта: create, update, duplicates, skipped, errors
    preview = models.JSONField("предпросмотр", default=dict, editable=False)
    result = models.JSONField("результат", default=dict, editable=False)
    # [{row, column, message}] — без значений из файла
    errors = models.JSONField("ошибки", default=list, editable=False)
    duplicates = models.JSONField("возможные дубли", default=list, editable=False)
    failure = models.CharField("причина сбоя", max_length=500, blank=True, editable=False)
    finished_at = models.DateTimeField("завершён", null=True, blank=True, editable=False)

    objects = ImportJobQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        verbose_name = "импорт"
        verbose_name_plural = "импорты"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} — {self.created_at:%d.%m.%Y %H:%M}"

    def get_absolute_url(self) -> str:
        return reverse("imports:detail", args=[self.pk])

    @property
    def full_name(self) -> str:
        return f"Импорт: {self.get_kind_display().lower()}"

    @property
    def in_progress(self) -> bool:
        return self.status in IN_PROGRESS

    @property
    def status_badge(self) -> str:
        return STATUS_BADGES.get(ImportStatus(self.status), "")
