"""Права на отчёты по матрице 2.2 ТЗ («Отчёты»; аналитику — агрегаты, без лишних ПДн).

| Роль          | По программе            | По волонтёрам, воронка | ФИО в отчётах |
|---------------|-------------------------|------------------------|---------------|
| Администратор | все данные              | да                     | да            |
| Координатор   | —                       | да                     | да            |
| Куратор       | только свои подопечные  | —                      | —             |
| Аналитик      | все данные              | да                     | нет           |
"""

from __future__ import annotations

from typing import Any

from apps.accounts.roles import RoleCode, user_has_role


def can_program_report(user: Any) -> bool:
    return user_has_role(user, RoleCode.ADMIN, RoleCode.ANALYST, RoleCode.CURATOR)


def program_report_all(user: Any) -> bool:
    """Отчёт по программе по всем подопечным, а не только по своим."""
    return user_has_role(user, RoleCode.ADMIN, RoleCode.ANALYST)


def can_volunteer_reports(user: Any) -> bool:
    return user_has_role(user, RoleCode.ADMIN, RoleCode.ANALYST, RoleCode.COORDINATOR)


def can_see_names(user: Any) -> bool:
    """Таблица по волонтёрам с ФИО — только тем, кто работает с карточками волонтёров."""
    return user_has_role(user, RoleCode.ADMIN, RoleCode.COORDINATOR)


def can_view_any(user: Any) -> bool:
    return can_program_report(user) or can_volunteer_reports(user)
