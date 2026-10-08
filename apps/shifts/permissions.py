"""Права на смены и назначения по матрице 2.2 ТЗ.

| Роль          | Смены и назначения                                          |
|---------------|-------------------------------------------------------------|
| Администратор | П — полный доступ                                           |
| Координатор   | П — полный доступ                                           |
| Куратор       | Ч — чтение; визиты — только к своим подопечным              |
| Аналитик      | Ч (агрегаты) — только в отчётах, не сами записи             |
| Волонтёр      | С — свои назначения, через Telegram-бота                    |
"""

from __future__ import annotations

from typing import Any

from apps.accounts.roles import RoleCode, user_has_role

MANAGE_ROLES: tuple[str, ...] = (RoleCode.ADMIN, RoleCode.COORDINATOR)
VIEW_ROLES: tuple[str, ...] = (*MANAGE_ROLES, RoleCode.CURATOR)


def can_view(user: Any) -> bool:
    return user_has_role(user, *VIEW_ROLES)


def can_manage(user: Any) -> bool:
    """Создание и изменение смен, назначения, отметка явки и часов."""
    return user_has_role(user, *MANAGE_ROLES)
