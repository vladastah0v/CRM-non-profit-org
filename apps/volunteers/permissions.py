"""Права на карточку волонтёра по матрице 2.2 ТЗ.

| Роль         | Карточка волонтёра                                   |
|--------------|------------------------------------------------------|
| Администратор| П — полный доступ                                    |
| Координатор  | П — полный доступ                                    |
| Куратор      | Ч (О) — только чтение ограниченного набора полей     |
| Аналитик     | — нет доступа                                        |
| Волонтёр     | С (О) — своя карточка, через бота                    |
"""

from __future__ import annotations

from typing import Any

from apps.accounts.roles import RoleCode, user_has_role

MANAGE_ROLES: tuple[str, ...] = (RoleCode.ADMIN, RoleCode.COORDINATOR)
VIEW_ROLES: tuple[str, ...] = (*MANAGE_ROLES, RoleCode.CURATOR)

# Ограниченный набор полей для куратора («Ч (О)»): чтобы связаться с волонтёром и
# подобрать его к подопечному. Без даты рождения, email, фото, согласий и истории.
LIMITED_FIELDS: tuple[str, ...] = (
    "id",
    "last_name",
    "first_name",
    "middle_name",
    "phone",
    "telegram_username",
    "district",
    "status",
    "is_archived",
)


def can_view(user: Any) -> bool:
    return user_has_role(user, *VIEW_ROLES)


def can_manage(user: Any) -> bool:
    """Создание, изменение, смена статуса, архивирование."""
    return user_has_role(user, *MANAGE_ROLES)


def sees_full_card(user: Any) -> bool:
    return can_manage(user)
