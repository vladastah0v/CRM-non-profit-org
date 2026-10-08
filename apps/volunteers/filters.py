"""Поиск и фильтры списка волонтёров (ФТ-В-05)."""

from __future__ import annotations

import datetime
from typing import Any, ClassVar

import django_filters
from django import forms
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.core.filters import PersonFilterSet
from apps.core.forms import TimeInput
from apps.core.models import District, Program, Skill

from . import activity
from .models import VolunteerStatus, Weekday

# Параметры, которые сохраняются в профиле (без текста поиска — в нём бывают ПДн)
SAVED_PARAMS = (
    "status",
    "skills",
    "district",
    "program",
    "weekday",
    "time",
    "last_shift",
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
    last_shift = django_filters.ChoiceFilter(
        label="Последняя смена",
        choices=[
            ("30", "За последние 30 дней"),
            ("60+", "Больше 60 дней назад или ни одной"),
            ("never", "Ни одной смены"),
            ("idle", f"Активные без смен больше {activity.IDLE_DAYS} дней"),
        ],
        empty_label="Не важно",
        method="filter_last_shift",
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

    def filter_last_shift(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        """По последней смене, на которой волонтёр отмечен как пришедший."""
        if value == "idle":  # как в сводке координатору (ФТ-У-05)
            return activity.idle(queryset)
        queryset = activity.with_last_shift(queryset)
        now = timezone.now()
        if value == "30":
            return queryset.filter(last_shift_at__gte=now - datetime.timedelta(days=30))
        if value == "60+":
            return queryset.filter(
                Q(last_shift_at__isnull=True)
                | Q(last_shift_at__lt=now - datetime.timedelta(days=60))
            )
        if value == "never":
            return queryset.filter(last_shift_at__isnull=True)
        return queryset

    def filter_duplicates(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        return queryset.filter(is_possible_duplicate=True) if value == "1" else queryset
