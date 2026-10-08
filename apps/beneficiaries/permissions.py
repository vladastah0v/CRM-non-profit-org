"""Права на карточку подопечного по матрице 2.2 и правилам 2.3 ТЗ.

| Роль          | Основные поля | Здоровье, инвалидность, документы | Согласия            |
|---------------|---------------|-----------------------------------|---------------------|
| Администратор | П             | П                                 | П                   |
| Координатор   | Ч (О)         | —                                 | —                   |
| Куратор       | С             | С                                 | П (свои подопечные) |
| Аналитик      | —             | —                                 | —                   |
| Волонтёр      | О (по назначению — бот) | —                       | —                   |

«Свои» подопечные куратора — где он ответственный или где руководитель открыл ему
доступ к случаю (п. 2.3). Права нескольких ролей суммируются.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from apps.accounts.roles import RoleCode, user_has_role

if TYPE_CHECKING:
    from .models import Beneficiary

# Набор «О» для координатора: связаться с подопечным и подобрать волонтёра для визита.
# Без даты рождения, программ, согласий, истории и защищённого блока.
LIMITED_FIELDS: tuple[str, ...] = (
    "id",
    "last_name",
    "first_name",
    "middle_name",
    "phone",
    "address",
    "category",
    "status",
    "curator",
    "is_archived",
)


def can_view_list(user: Any) -> bool:
    return user_has_role(user, RoleCode.ADMIN, RoleCode.CURATOR, RoleCode.COORDINATOR)


def can_create(user: Any) -> bool:
    return user_has_role(user, RoleCode.ADMIN, RoleCode.CURATOR)


def is_admin(user: Any) -> bool:
    """«Руководитель» из п. 2.3: назначает кураторов и открывает доступ второму куратору."""
    return user_has_role(user, RoleCode.ADMIN)


def manages(user: Any, beneficiary: Beneficiary) -> bool:
    """Полный доступ к карточке: администратор или куратор своего подопечного.

    Тот же уровень даёт доступ к защищённому блоку (здоровье, документы).
    """
    if is_admin(user):
        return True
    if not user_has_role(user, RoleCode.CURATOR):
        return False
    from .models import Beneficiary

    return Beneficiary.objects.owned_by(user).filter(pk=beneficiary.pk).exists()
