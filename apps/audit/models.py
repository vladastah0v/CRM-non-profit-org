"""Журнал аудита (НФТ-ИБ-07). Только добавление записей.

В журнал не пишутся значения полей и имена людей — только тип и id объекта и названия
изменённых полей. Так журнал не нужно обезличивать вместе с карточкой (ФТ-А-04).
Исключение из правила created_by/updated_by п. 4.1: запись неизменяема, автор — поле user.
"""

import uuid
from typing import Any, NoReturn, Self

from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditAction(models.TextChoices):
    LOGIN = "login", "Вход"
    LOGOUT = "logout", "Выход"
    LOGIN_FAILED = "login_failed", "Неудачный вход"
    ACCOUNT_LOCKED = "account_locked", "Блокировка после неудачных попыток"
    OTP_FAILED = "otp_failed", "Неверный код 2FA"
    TWO_FACTOR_ENABLED = "2fa_enabled", "Подключена 2FA"
    TWO_FACTOR_RESET = "2fa_reset", "Сброшена 2FA"
    PASSWORD_CHANGED = "password_changed", "Смена пароля"
    USER_BLOCKED = "user_blocked", "Доступ заблокирован"
    USER_UNBLOCKED = "user_unblocked", "Доступ разблокирован"
    PERMISSION_CHANGE = "permission_change", "Изменение прав"
    VIEW_SENSITIVE = "view_sensitive", "Просмотр чувствительных данных"
    EXPORT = "export", "Выгрузка"
    FILE_DOWNLOAD = "file_download", "Скачивание файла"
    CREATE = "create", "Создание"
    UPDATE = "update", "Изменение"
    ARCHIVE = "archive", "Архивирование"
    RESTORE = "restore", "Восстановление из архива"
    ANONYMIZE = "anonymize", "Обезличивание"


class AuditLogQuerySet(models.QuerySet):
    def visible_to(self, user: Any) -> Self:
        from apps.accounts.roles import RoleCode, user_has_role

        # Журнал аудита — только чтение и только администратор (матрица 2.2)
        return self.all() if user_has_role(user, RoleCode.ADMIN) else self.none()

    def update(self, **kwargs: Any) -> NoReturn:
        raise PermissionError("Журнал аудита нельзя изменять")

    def delete(self) -> NoReturn:
        raise PermissionError("Записи журнала аудита удаляются только по истечении срока хранения")


class AuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField("время", default=timezone.now, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="пользователь",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    username = models.CharField("логин", max_length=150, blank=True)
    action = models.CharField("действие", max_length=30, choices=AuditAction.choices, db_index=True)
    object_type = models.CharField("тип объекта", max_length=100, blank=True, db_index=True)
    object_id = models.CharField("id объекта", max_length=64, blank=True, db_index=True)
    fields = models.JSONField("поля", default=list, blank=True)
    ip = models.GenericIPAddressField("IP-адрес", null=True, blank=True)
    user_agent = models.CharField("браузер", max_length=255, blank=True)
    extra = models.JSONField("подробности", default=dict, blank=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        verbose_name = "запись журнала аудита"
        verbose_name_plural = "журнал аудита"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["object_type", "object_id", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.created_at:%d.%m.%Y %H:%M} {self.username} {self.get_action_display()}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise PermissionError("Журнал аудита нельзя изменять")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise PermissionError("Записи журнала аудита удаляются только по истечении срока хранения")
