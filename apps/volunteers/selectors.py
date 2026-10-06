"""Выборки для списка и карточки волонтёра — только через visible_to(user) (НФТ-ИБ-08)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import QueryDict
from django.shortcuts import get_object_or_404

from apps.consents.models import Consent

from . import permissions
from .filters import VolunteerFilter
from .models import (
    Volunteer,
    VolunteerHours,
    VolunteerStatus,
    VolunteerStatusChange,
    VolunteerTraining,
)
from .services import activation_problems, allowed_transitions, total_hours


def volunteer_list(user: Any, params: QueryDict) -> VolunteerFilter:
    full = permissions.sees_full_card(user)
    queryset = Volunteer.objects.visible_to(user).select_related("district")
    if full:
        queryset = queryset.prefetch_related("skills", "programs")
    else:
        queryset = queryset.prefetch_related("skills")
    return VolunteerFilter(params, queryset=queryset, full_access=full)


def get_volunteer(user: Any, pk: Any) -> Volunteer:
    """Карточка с проверкой прав: чужая или недоступная — 404, как несуществующая."""
    queryset: QuerySet[Volunteer] = Volunteer.objects.visible_to(user).select_related("district")
    if permissions.sees_full_card(user):
        queryset = queryset.select_related(
            "source", "leaving_reason", "photo", "created_by"
        ).prefetch_related("skills", "programs", "availability")
    else:
        queryset = queryset.prefetch_related("skills")
    return get_object_or_404(queryset, pk=pk)


def get_managed_volunteer(user: Any, pk: Any) -> Volunteer:
    """Карточка для изменения: только роли с полным доступом (матрица 2.2, «П»).

    Отказ — до поиска записи: по ответу нельзя понять, существует ли карточка.
    """
    if not permissions.can_manage(user):
        raise PermissionDenied
    return get_object_or_404(Volunteer.objects.visible_to(user), pk=pk)


@dataclass
class VolunteerCard:
    volunteer: Volunteer
    full: bool
    can_manage: bool
    consents: list[Consent]
    trainings: list[VolunteerTraining]
    hours: list[VolunteerHours]
    total_hours: Decimal
    status_changes: list[VolunteerStatusChange]
    transitions: list[str]
    activation_problems: list[str]

    @property
    def show_activation_hint(self) -> bool:
        """Подсказка, чего не хватает: когда следующий шаг — статус «Активен»."""
        return bool(self.activation_problems) and VolunteerStatus.ACTIVE in self.transitions


def volunteer_card(user: Any, volunteer: Volunteer) -> VolunteerCard:
    full = permissions.sees_full_card(user)
    if not full:
        return VolunteerCard(volunteer, False, False, [], [], [], Decimal(0), [], [], [])
    consents = list(
        Consent.objects.visible_to(user)
        .filter(volunteer=volunteer, is_archived=False)
        .select_related("scan")
    )
    trainings = list(volunteer.trainings.filter(is_archived=False).select_related("training_type"))
    hours = list(volunteer.manual_hours.filter(is_archived=False).select_related("program")[:50])
    changes = list(volunteer.status_changes.select_related("reason", "created_by")[:50])
    can_manage = permissions.can_manage(user)
    return VolunteerCard(
        volunteer=volunteer,
        full=True,
        can_manage=can_manage,
        consents=consents,
        trainings=trainings,
        hours=hours,
        total_hours=total_hours(volunteer),
        status_changes=changes,
        transitions=allowed_transitions(volunteer) if not volunteer.is_archived else [],
        activation_problems=activation_problems(volunteer),
    )
