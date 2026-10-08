"""Список смен: ближайшие по умолчанию, фильтры по типу, программе, району и укомплектованности."""

from __future__ import annotations

from typing import Any

import django_filters
from django import forms
from django.db.models import Count, F, Q, QuerySet
from django.utils import timezone

from apps.core.filters import StyledForm
from apps.core.forms import DateInput
from apps.core.models import District, Program, ShiftType

from .models import OCCUPYING_STATUSES, ShiftStatus

SAVED_PARAMS = ("period", "shift_type", "program", "district", "staffing", "cancelled")


def with_occupied(queryset: QuerySet) -> QuerySet:
    """Сколько мест занято: назначен, подтвердил, пришёл."""
    return queryset.annotate(
        occupied=Count(
            "assignments",
            filter=Q(assignments__status__in=OCCUPYING_STATUSES, assignments__is_archived=False),
        )
    )


class ShiftFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(label="Название или место", method="search")
    period = django_filters.ChoiceFilter(
        label="Когда",
        choices=[("upcoming", "Предстоящие"), ("past", "Прошедшие")],
        empty_label="Все",
        method="filter_period",
    )
    date_from = django_filters.DateFilter(
        label="С даты", field_name="starts_at", lookup_expr="date__gte", widget=DateInput()
    )
    date_to = django_filters.DateFilter(
        label="По дату", field_name="starts_at", lookup_expr="date__lte", widget=DateInput()
    )
    shift_type = django_filters.ModelChoiceFilter(
        label="Тип", queryset=ShiftType.objects.active(), empty_label="Любой"
    )
    program = django_filters.ModelChoiceFilter(
        label="Программа", queryset=Program.objects.active(), empty_label="Любая"
    )
    district = django_filters.ModelChoiceFilter(
        label="Район", queryset=District.objects.active(), empty_label="Любой"
    )
    staffing = django_filters.ChoiceFilter(
        label="Укомплектованность",
        choices=[("short", "Не хватает волонтёров"), ("full", "Набраны")],
        empty_label="Любая",
        method="filter_staffing",
    )
    cancelled = django_filters.BooleanFilter(
        label="Показывать отменённые", method="filter_cancelled", widget=forms.CheckboxInput
    )

    class Meta:
        form = StyledForm
        fields: list[str] = []

    def __init__(self, data: Any = None, *args: Any, **kwargs: Any) -> None:
        super().__init__(data, *args, **kwargs)
        self._show_cancelled = bool(self.data.get("cancelled"))

    @property
    def qs(self) -> QuerySet:
        queryset = super().qs
        if not self._show_cancelled:
            queryset = queryset.filter(status=ShiftStatus.PLANNED)
        if self.data.get("period") == "past":
            return queryset.order_by("-starts_at")
        return queryset.order_by("starts_at")

    def search(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        value = " ".join(value.split())
        if not value:
            return queryset
        return queryset.filter(Q(title__icontains=value) | Q(address__icontains=value))

    def filter_period(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        now = timezone.now()
        if value == "upcoming":
            return queryset.filter(ends_at__gt=now)
        if value == "past":
            return queryset.filter(ends_at__lte=now)
        return queryset

    def filter_staffing(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        """Выборка приходит уже с аннотацией occupied (with_occupied во view)."""
        if value == "short":
            return queryset.filter(occupied__lt=F("volunteers_needed"))
        if value == "full":
            return queryset.filter(occupied__gte=F("volunteers_needed"))
        return queryset

    def filter_cancelled(self, queryset: QuerySet, name: str, value: bool) -> QuerySet:
        return queryset  # учтено в qs: по умолчанию отменённые скрыты
