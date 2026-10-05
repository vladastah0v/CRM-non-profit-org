"""Роли пользователей (раздел 2 ТЗ).

Один пользователь может иметь несколько ролей — права суммируются.
"""

from __future__ import annotations

from typing import Any

from django.db import models


class RoleCode(models.TextChoices):
    ADMIN = "admin", "Администратор"
    COORDINATOR = "coordinator", "Координатор волонтёров"
    CURATOR = "curator", "Куратор"
    ANALYST = "analyst", "Аналитик / отчётность"
    VOLUNTEER = "volunteer", "Волонтёр"


# Роли сотрудников фонда — у них есть доступ к веб-интерфейсу.
# Волонтёр работает только через Telegram-бота (п. 2.1).
STAFF_ROLES: tuple[str, ...] = (
    RoleCode.ADMIN,
    RoleCode.COORDINATOR,
    RoleCode.CURATOR,
    RoleCode.ANALYST,
)

_CACHE_ATTR = "_role_codes_cache"


def user_role_codes(user: Any) -> frozenset[str]:
    """Коды ролей пользователя. Кэшируются на объекте на время запроса."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return frozenset()
    cached = getattr(user, _CACHE_ATTR, None)
    if cached is None:
        codes = set(user.roles.values_list("code", flat=True))
        if user.is_superuser:
            codes.add(RoleCode.ADMIN)
        cached = frozenset(codes)
        setattr(user, _CACHE_ATTR, cached)
    return cached


def user_has_role(user: Any, *roles: str) -> bool:
    return bool(user_role_codes(user).intersection(roles))


def is_staff_member(user: Any) -> bool:
    return user_has_role(user, *STAFF_ROLES)


def invalidate_role_cache(user: Any) -> None:
    if hasattr(user, _CACHE_ATTR):
        delattr(user, _CACHE_ATTR)
