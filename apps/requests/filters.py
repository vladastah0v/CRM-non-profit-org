"""Список заявок: «Мои заявки» куратора с сортировкой по сроку (ФТ-З-04)."""

from __future__ import annotations

from typing import Any

import django_filters
from django import forms
from django.db.models import Case, F, IntegerField, Q, QuerySet, Value, When
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.roles import RoleCode
from apps.beneficiaries.models import Beneficiary
from apps.core.filters import StyledForm
from apps.core.models import AssistanceType
from apps.core.normalize import normalize_name

from .models import OPEN_STATUSES, RequestStatus, Urgency

SAVED_PARAMS = ("scope", "status", "overdue", "urgency", "assistance_type", "responsible", "sort")

URGENCY_RANK = Case(
    When(urgency=Urgency.EMERGENCY, then=Value(0)),
    When(urgency=Urgency.URGENT, then=Value(1)),
    default=Value(2),
    output_field=IntegerField(),
)
SORTS: dict[str, tuple[str, list[Any]]] = {
    "due": ("По сроку", [F("due_date").asc(nulls_last=True), URGENCY_RANK, "-created_at"]),
    "urgency": ("По срочности", [URGENCY_RANK, F("due_date").asc(nulls_last=True)]),
    "-created": ("Сначала новые", ["-created_at"]),
}
DEFAULT_SORT = "due"


class RequestFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(label="Подопечный", method="search")
    scope = django_filters.ChoiceFilter(
        label="Чьи",
        choices=[("mine", "Мои заявки")],
        empty_label="Все доступные",
        method="filter_scope",
    )
    status = django_filters.ChoiceFilter(
        label="Статус",
        choices=[("open", "Открытые"), *RequestStatus.choices],
        empty_label="Все статусы",
        method="filter_status",
    )
    overdue = django_filters.BooleanFilter(
        label="Только просроченные",
        method="filter_overdue",
        widget=forms.CheckboxInput,
    )
    urgency = django_filters.ChoiceFilter(
        label="Срочность", choices=Urgency.choices, empty_label="Любая"
    )
    assistance_type = django_filters.ModelChoiceFilter(
        label="Тип помощи", queryset=AssistanceType.objects.active(), empty_label="Любой"
    )
    responsible = django_filters.ModelChoiceFilter(
        label="Ответственный",
        queryset=User.objects.filter(
            is_active=True, roles__code__in=[RoleCode.CURATOR, RoleCode.ADMIN]
        ).distinct(),
        empty_label="Любой",
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

    def __init__(self, *args: Any, user: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.user = user

    @property
    def qs(self) -> QuerySet:
        queryset = super().qs.filter(is_archived=False)
        data = self.form.cleaned_data if self.form.is_bound and self.is_valid() else {}
        if not data.get("sort"):
            queryset = queryset.order_by(*SORTS[DEFAULT_SORT][1])
        return queryset.distinct()

    def search(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        words = normalize_name(value).split()
        if not words:
            return queryset
        condition = Q()
        for word in words:
            condition &= Q(beneficiary__search_name__contains=word)
        return queryset.filter(condition)

    def filter_scope(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        if value != "mine":
            return queryset
        own = Beneficiary.objects.owned_by(self.user)
        return queryset.filter(Q(responsible=self.user) | Q(beneficiary__in=own))

    def filter_status(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        if value == "open":
            return queryset.filter(status__in=OPEN_STATUSES)
        return queryset.filter(status=value) if value else queryset

    def filter_overdue(self, queryset: QuerySet, name: str, value: bool | None) -> QuerySet:
        if not value:
            return queryset
        return queryset.filter(status__in=OPEN_STATUSES, due_date__lt=timezone.localdate())

    def apply_sort(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        return queryset.order_by(*SORTS.get(value, SORTS[DEFAULT_SORT])[1])
