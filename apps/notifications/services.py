from __future__ import annotations

from collections.abc import Iterable

from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.roles import RoleCode
from apps.core.context import get_current_user

from .models import Notification, NotificationKind


def users_with_role(*roles: str) -> list[User]:
    """Активные пользователи с любой из ролей. Суперпользователь считается администратором."""
    users = User.objects.filter(is_active=True, roles__code__in=roles)
    if RoleCode.ADMIN in roles:
        users = users | User.objects.filter(is_active=True, is_superuser=True)
    return list(users.distinct())


def notify(
    recipients: Iterable[User],
    kind: NotificationKind,
    title: str,
    *,
    body: str = "",
    url: str = "",
) -> list[Notification]:
    """Создать уведомления. Не передавайте в title/body ФИО и контакты — только ссылку."""
    # bulk_create не вызывает save(), поэтому автор проставляется явно
    author = get_current_user()
    return Notification.objects.bulk_create(
        [
            Notification(
                recipient=user,
                kind=kind,
                title=title,
                body=body,
                url=url,
                created_by=author,
                updated_by=author,
            )
            for user in recipients
        ]
    )


def mark_read(notification: Notification) -> None:
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at"])


def mark_all_read(user: User) -> int:
    now = timezone.now()
    return (
        Notification.objects.visible_to(user)
        .unread()
        .update(read_at=now, updated_at=now, updated_by=user)
    )


def unread_count(user: User) -> int:
    return Notification.objects.visible_to(user).unread().count()
