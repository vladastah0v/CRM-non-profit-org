"""Последняя смена волонтёра: фильтр списка (ФТ-В-05) и сводка «без смен» (ФТ-У-05)."""

from __future__ import annotations

import datetime
from typing import Any

from django.db.models import Max, Q, QuerySet
from django.utils import timezone

from apps.shifts.models import AssignmentKind, AssignmentStatus

from .models import Volunteer, VolunteerStatus

IDLE_DAYS = 60


def with_last_shift(queryset: QuerySet[Volunteer]) -> QuerySet[Any]:
    """last_shift_at — начало последней смены, на которой волонтёр отмечен как пришедший.
    Тип QuerySet[Any]: иначе mypy не видит аннотацию в фильтрах по last_shift_at."""
    return queryset.annotate(
        last_shift_at=Max(
            "assignments__shift__starts_at",
            filter=Q(
                assignments__kind=AssignmentKind.SHIFT,
                assignments__status=AssignmentStatus.ATTENDED,
                assignments__is_archived=False,
            ),
        )
    )


def idle(queryset: QuerySet[Volunteer], now: datetime.datetime | None = None) -> QuerySet[Any]:
    """Активные волонтёры без смен больше IDLE_DAYS дней. Отсчёт — от последней смены, а если
    смен не было или волонтёр недавно вернулся с паузы — от перехода в «Активен»."""
    threshold = (now or timezone.now()) - datetime.timedelta(days=IDLE_DAYS)
    return with_last_shift(
        queryset.filter(status=VolunteerStatus.ACTIVE, status_changed_at__lt=threshold)
    ).filter(Q(last_shift_at__isnull=True) | Q(last_shift_at__lt=threshold))
