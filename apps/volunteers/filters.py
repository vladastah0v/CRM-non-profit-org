"""Поиск и фильтры списка волонтёров (ФТ-В-05).

Фильтр «дата последней смены» появится вместе с модулем «Смены» (этап 7).
"""

from __future__ import annotations

from typing import Any

import django_filters
from django import forms
from django.db.models import Q, QuerySet

from apps.core.forms import StyledFormMixin
from apps.core.models import District, Program, Skill
from apps.core.normalize import normalize_name, phone_search_digits

from .models import VolunteerStatus, Weekday

SORTS: dict[str, tuple[str, list[str]]] = {
    "name": ("ФИО (А–Я)", ["last_name", "first_name", "middle_name"]),
    "-name": ("ФИО (Я–А)", ["-last_name", "-first_name", "-middle_name"]),
    "status": ("Статус", ["status", "last_name", "first_name"]),
    "-created": ("Сначала новые", ["-created_at"]),
    "-status_changed": ("Недавняя смена статуса", ["-status_changed_at"]),
}
DEFAULT_SORT = "name"

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
# Куратору доступны только фильтры по полям ограниченного набора «О»
CURATOR_FILTERS = ("q", "status", "skills", "district", "sort")


class StyledForm(StyledFormMixin, forms.Form):
    pass


class VolunteerFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(label="Поиск", method="search")
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
        label="в … часов",
        method="filter_availability",
        widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
    )
    duplicates = django_filters.ChoiceFilter(
        label="Дубли",
        choices=[("1", "Только возможные дубли")],
        empty_label="Все карточки",
        method="filter_duplicates",
    )
    archived = django_filters.ChoiceFilter(
        label="Архив",
        choices=[("1", "Только архивные"), ("all", "Все, включая архивные")],
        empty_label="Без архивных",
        method="filter_archived",
    )
    sort = django_filters.ChoiceFilter(
        label="Сортировка",
        choices=[(key, label) for key, (label, _) in SORTS.items()],
        empty_label=None,
        method="apply_sort",
    )

    class Meta:
        form = StyledForm
        fields: list[str] = []

    def __init__(self, *args: Any, full_access: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.full_access = full_access
        if not full_access:
            for name in list(self.filters):
                if name not in CURATOR_FILTERS:
                    del self.filters[name]

    @property
    def qs(self) -> QuerySet:
        queryset = super().qs
        data = self.form.cleaned_data if self.form.is_bound and self.is_valid() else {}
        if "archived" not in self.filters or not data.get("archived"):
            queryset = queryset.filter(is_archived=False)
        if not data.get("sort"):
            queryset = queryset.order_by(*SORTS[DEFAULT_SORT][1])
        return queryset.distinct()

    def search(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        """Поиск по ФИО (части в любом порядке) и телефону; координатору — и по email."""
        value = value.strip()
        if not value:
            return queryset
        condition = Q()
        words = normalize_name(value).split()
        if words:
            by_name = Q()
            for word in words:
                by_name &= Q(search_name__contains=word)
            condition |= by_name
        digits = phone_search_digits(value)
        if len(digits) >= 4:
            condition |= Q(phone__contains=digits)
        if self.full_access and "@" in value:
            condition |= Q(email__icontains=value)
        return queryset.filter(condition)

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

    def filter_archived(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        if value == "1":
            return queryset.filter(is_archived=True)
        return queryset  # «all» — без ограничения

    def apply_sort(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        return queryset.order_by(*SORTS.get(value, SORTS[DEFAULT_SORT])[1])
