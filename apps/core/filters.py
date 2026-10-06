"""Основа фильтров списков людей (волонтёры, подопечные): поиск по ФИО и телефону,
архив, сортировка (п. 3.10 ТЗ)."""

from __future__ import annotations

from typing import Any, ClassVar

import django_filters
from django import forms
from django.db.models import Q, QuerySet

from .forms import StyledFormMixin
from .normalize import normalize_name, phone_search_digits

PERSON_SORTS: dict[str, tuple[str, list[str]]] = {
    "name": ("ФИО (А–Я)", ["last_name", "first_name", "middle_name"]),
    "-name": ("ФИО (Я–А)", ["-last_name", "-first_name", "-middle_name"]),
    "status": ("Статус", ["status", "last_name", "first_name"]),
    "-created": ("Сначала новые", ["-created_at"]),
    "-status_changed": ("Недавняя смена статуса", ["-status_changed_at"]),
}
DEFAULT_SORT = "name"


class StyledForm(StyledFormMixin, forms.Form):
    pass


class PersonFilterSet(django_filters.FilterSet):
    """Подклассы задают свои фильтры и `limited_filters` — набор для роли с ограниченным
    доступом (поля «О» матрицы 2.2). Архивные записи скрыты, пока не выбран фильтр архива."""

    limited_filters: ClassVar[tuple[str, ...]] = ("q", "status", "sort")

    q = django_filters.CharFilter(label="Поиск", method="search")
    archived = django_filters.ChoiceFilter(
        label="Архив",
        choices=[("1", "Только архивные"), ("all", "Все, включая архивные")],
        empty_label="Без архивных",
        method="filter_archived",
    )
    sort = django_filters.ChoiceFilter(
        label="Сортировка",
        choices=[(key, label) for key, (label, _) in PERSON_SORTS.items()],
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
                if name not in self.limited_filters:
                    del self.filters[name]

    @property
    def qs(self) -> QuerySet:
        queryset = super().qs
        data = self.form.cleaned_data if self.form.is_bound and self.is_valid() else {}
        if "archived" not in self.filters or not data.get("archived"):
            queryset = queryset.filter(is_archived=False)
        if not data.get("sort"):
            queryset = queryset.order_by(*PERSON_SORTS[DEFAULT_SORT][1])
        return queryset.distinct()

    def search_condition(self, value: str) -> Q:
        """ФИО — части в любом порядке; телефон — от 4 цифр, «8 916…» = «+7 916…»."""
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
        return condition

    def search(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        value = value.strip()
        return queryset.filter(self.search_condition(value)) if value else queryset

    def filter_archived(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        return queryset.filter(is_archived=True) if value == "1" else queryset

    def apply_sort(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        return queryset.order_by(*PERSON_SORTS.get(value, PERSON_SORTS[DEFAULT_SORT])[1])
