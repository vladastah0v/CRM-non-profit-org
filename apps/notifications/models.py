"""Уведомления сотрудникам внутри интерфейса (раздел 3.8 ТЗ).

Текст уведомления не содержит ФИО и контактов: только что произошло и ссылка на
карточку. Карточка открывается с обычной проверкой прав.
"""

from typing import Any, Self

from django.conf import settings
from django.db import models

from apps.core.models import ArchivableQuerySet, BaseModel


class NotificationKind(models.TextChoices):
    NEW_VOLUNTEER_APPLICATION = "new_volunteer_application", "Новая анкета волонтёра"
    CONSENT_REVOKED = "consent_revoked", "Отзыв согласия"
    CASE_ASSIGNED = "case_assigned", "Назначен случай подопечного"
    REQUEST_ASSIGNED = "request_assigned", "Назначена заявка"
    REQUEST_DUE = "request_due", "Срок заявки"
    TASK_ASSIGNED = "task_assigned", "Назначена задача"
    TASK_DUE = "task_due", "Срок задачи"
    ASSIGNMENT_DECLINED = "assignment_declined", "Волонтёр отказался от смены или визита"


class NotificationQuerySet(ArchivableQuerySet):
    def visible_to(self, user: Any) -> Self:
        """Каждый видит только свои уведомления."""
        if not getattr(user, "is_authenticated", False):
            return self.none()
        return self.filter(recipient=user)

    def unread(self) -> Self:
        return self.filter(read_at__isnull=True)


class Notification(BaseModel):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    kind = models.CharField("тип", max_length=50, choices=NotificationKind.choices)
    title = models.CharField("заголовок", max_length=200)
    body = models.CharField("текст", max_length=500, blank=True)
    url = models.CharField("ссылка", max_length=300, blank=True)
    read_at = models.DateTimeField("прочитано", null=True, blank=True)

    objects = NotificationQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        verbose_name = "уведомление"
        verbose_name_plural = "уведомления"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["recipient", "-created_at"],
                condition=models.Q(read_at__isnull=True),
                name="notification_unread_idx",
            )
        ]

    def __str__(self) -> str:
        return self.title
