"""Поиск и фильтры списка подопечных (п. 3.10)."""

from __future__ import annotations

from typing import Any, ClassVar

import django_filters

from apps.accounts.models import User
from apps.accounts.roles import RoleCode
from apps.core.filters import PersonFilterSet
from apps.core.models import BeneficiaryCategory, Program

from .models import CaseStatus

SAVED_PARAMS = ("status", "category", "program", "curator", "archived", "sort")


class BeneficiaryFilter(PersonFilterSet):
    # Координатору (набор «О») — только фильтры по полям этого набора
    limited_filters: ClassVar[tuple[str, ...]] = ("q", "status", "category", "sort")

    status = django_filters.ChoiceFilter(
        label="Статус случая", choices=CaseStatus.choices, empty_label="Все статусы"
    )
    category = django_filters.ModelChoiceFilter(
        label="Категория", queryset=BeneficiaryCategory.objects.active(), empty_label="Все"
    )
    program = django_filters.ModelChoiceFilter(
        label="Программа",
        field_name="programs",
        queryset=Program.objects.active(),
        empty_label="Все программы",
    )
    curator = django_filters.ModelChoiceFilter(
        label="Куратор",
        queryset=User.objects.filter(is_active=True, roles__code=RoleCode.CURATOR).distinct(),
        empty_label="Все кураторы",
    )

    def __init__(self, *args: Any, choose_curator: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not choose_curator:
            self.filters.pop("curator", None)
