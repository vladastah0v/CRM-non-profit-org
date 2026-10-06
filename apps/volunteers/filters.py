"""Поиск и фильтры списка волонтёров (ФТ-В-05).

Фильтр «дата последней смены» появится вместе с модулем «Смены» (этап 7).
"""

from __future__ import annotations

from typing import Any, ClassVar

import django_filters
from django import forms
from django.db.models import Q, QuerySet

from apps.core.filters import PersonFilterSet
from apps.core.forms import TimeInput
from apps.core.models import District, Program, Skill

from .models import VolunteerStatus, Weekday

# Параметры, которые сохраняются в профиле (без текста поиска — в нём бывают ПДн)
SAVED_PARAMS = (
    "status",
    "skills",
    "district",
    "program",
    "weekday",
    "time",
    "duplicates",
    "archived",
    "sort",
)


class VolunteerFilter(PersonFilterSet):
    # Куратору доступны только фильтры по полям ограниченного набора «О»
    limited_filters: ClassVar[tuple[str, ...]] = ("q", "status", "skills", "district", "sort")

    status = django_filters.ChoiceFilter(
        label="Статус", choices=VolunteerStatus.choices, empty_label="Все статусы"
    )
    skills = django_filters.ModelMultipleChoiceFilter(
        label="Навыки (все отмеченные)",
        queryset=Skill.objects.active(),
        conjoined=True,
        widget=forms.CheckboxSelectMultiple,
    )
    district = django_filters.ModelChoiceFilter(
        label="Район", queryset=District.objects.active(), empty_label="Все районы"
    )
    program = django_filters.ModelChoiceFilter(
        label="Программа",
        field_name="programs",
        queryset=Program.objects.active(),
        empty_label="Все программы",
    )
    weekday = django_filters.ChoiceFilter(
        label="Доступен в день",
        choices=Weekday.choices,
        empty_label="Любой день",
        method="filter_availability",
    )
    time = django_filters.TimeFilter(
        label="в … часов", method="filter_availability", widget=TimeInput()
    )
    duplicates = django_filters.ChoiceFilter(
        label="Дубли",
        choices=[("1", "Только возможные дубли")],
        empty_label="Все карточки",
        method="filter_duplicates",
    )

    def search_condition(self, value: str) -> Q:
        """Плюс поиск по email — только при полном доступе (email вне набора «О»)."""
        condition = super().search_condition(value)
        if self.full_access and "@" in value:
            condition |= Q(email__icontains=value)
        return condition

    def filter_availability(self, queryset: QuerySet, name: str, value: Any) -> QuerySet:
        """День и время обрабатываются вместе: «доступен в среду в 18:00»."""
        data = self.form.cleaned_data
        weekday, time = data.get("weekday"), data.get("time")
        if name == "time" and weekday not in (None, ""):
            return queryset  # уже учтено при обработке дня
        condition = Q()
        if weekday not in (None, ""):
            condition &= Q(availability__weekday=int(weekday))
        if time is not None:
            condition &= Q(availability__time_from__lte=time, availability__time_to__gt=time)
        return queryset.filter(condition) if condition else queryset

    def filter_duplicates(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        return queryset.filter(is_possible_duplicate=True) if value == "1" else queryset
