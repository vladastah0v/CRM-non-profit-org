"""Права на заявки и оказанную помощь по матрице 2.2 ТЗ.

| Роль          | Заявки | Оказанная помощь |
|---------------|--------|------------------|
| Администратор | П      | П                |
| Координатор   | Ч      | Ч                |
| Куратор       | С      | С                |
| Аналитик      | —      | Ч (агрегаты — в отчётах)         |
| Волонтёр      | —      | —                |

«Свои» у куратора: заявки и помощь его подопечных (п. 2.3) и заявки, где он ответственный.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from apps.accounts.roles import RoleCode, user_has_role
from apps.beneficiaries import permissions as beneficiary_permissions

if TYPE_CHECKING:
    from apps.beneficiaries.models import Beneficiary

    from .models import HelpRequest


def can_view(user: Any) -> bool:
    return user_has_role(user, RoleCode.ADMIN, RoleCode.CURATOR, RoleCode.COORDINATOR)


def manages_beneficiary(user: Any, beneficiary: Beneficiary) -> bool:
    """Создавать заявки и регистрировать помощь: администратор и куратор подопечного."""
    return beneficiary_permissions.manages(user, beneficiary)


def manages_request(user: Any, help_request: HelpRequest) -> bool:
    if user_has_role(user, RoleCode.ADMIN):
        return True
    if not user_has_role(user, RoleCode.CURATOR):
        return False
    return help_request.responsible_id == user.pk or manages_beneficiary(
        user, help_request.beneficiary
    )


def can_register_bulk(user: Any) -> bool:
    """ФТ-З-05: массовая регистрация — администратор (любые подопечные) и куратор (свои)."""
    return user_has_role(user, RoleCode.ADMIN, RoleCode.CURATOR)
