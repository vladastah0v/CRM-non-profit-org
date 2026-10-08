"""Выборки для экранов смен и назначений — только через visible_to(user) (НФТ-ИБ-08)."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Exists, IntegerField, OuterRef, Q, QuerySet, Value
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.beneficiaries.models import Beneficiary, CaseStatus
from apps.core.normalize import normalize_name
from apps.volunteers.models import Volunteer, VolunteerAvailability, VolunteerStatus
from apps.volunteers.services import shift_hours_total

from . import permissions, services
from .models import (
    OCCUPYING_STATUSES,
    PENDING_STATUSES,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    AssignmentStatusChange,
    Shift,
)

CANDIDATES_LIMIT = 100


def get_shift(user: Any, pk: Any) -> Shift:
    if not permissions.can_view(user):
        raise PermissionDenied
    queryset = Shift.objects.visible_to(user).select_related(
        "shift_type", "district", "program", "created_by"
    )
    return get_object_or_404(queryset, pk=pk)


def get_managed_shift(user: Any, pk: Any) -> Shift:
    """Для изменения — только «П»-роли; отказ до поиска записи."""
    if not permissions.can_manage(user):
        raise PermissionDenied
    return get_shift(user, pk)


@dataclass
class AssignmentRow:
    assignment: Assignment
    transitions: list[str]


@dataclass
class ShiftCard:
    shift: Shift
    can_manage: bool
    rows: list[AssignmentRow]
    occupied: int
    history: Sequence[AssignmentStatusChange]

    @property
    def free(self) -> int:
        return max(self.shift.volunteers_needed - self.occupied, 0)

    @property
    def can_mark_attendance(self) -> bool:
        return (
            self.can_manage
            and self.shift.has_started
            and not self.shift.is_cancelled
            and not self.shift.is_archived
            and any(row.assignment.status in OCCUPYING_STATUSES for row in self.rows)
        )

    @property
    def can_assign(self) -> bool:
        return self.can_manage and not self.shift.is_cancelled and not self.shift.is_archived


def shift_card(user: Any, shift: Shift) -> ShiftCard:
    can_manage = permissions.can_manage(user)
    assignments = list(
        Assignment.objects.visible_to(user)
        .filter(shift=shift, is_archived=False)
        .select_related("volunteer")
        .order_by("volunteer__last_name", "volunteer__first_name")
    )
    for assignment in assignments:
        assignment.shift = shift
    rows = [
        AssignmentRow(a, services.allowed_transitions(a) if can_manage else []) for a in assignments
    ]
    occupied = sum(1 for a in assignments if a.status in OCCUPYING_STATUSES)
    history = list(
        AssignmentStatusChange.objects.filter(
            assignment__shift=shift, assignment__in=[a.pk for a in assignments]
        ).select_related("assignment__volunteer", "created_by")[:50]
    )
    return ShiftCard(shift, can_manage, rows, occupied, history)


# ---------------------------------------------------------------------------
# Подбор волонтёров (ФТ-С-03)
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    volunteer: Volunteer
    has_skills: bool
    same_district: bool
    available: bool

    @property
    def score(self) -> int:
        return int(self.has_skills) * 4 + int(self.available) * 2 + int(self.same_district)


def _availability_condition(shift: Shift) -> Q:
    """Слот доступности в день смены покрывает её время (смена через полночь — начало)."""
    start = timezone.localtime(shift.starts_at)
    end = timezone.localtime(shift.ends_at)
    slot = Q(volunteer=OuterRef("pk"), weekday=start.weekday(), time_from__lte=start.time())
    if end.date() == start.date():
        slot &= Q(time_to__gte=end.time())
    else:
        slot &= Q(time_to__gt=start.time())
    return slot


def suggest_volunteers(
    shift: Shift,
    *,
    query: str = "",
    only_skills: bool = True,
    only_district: bool = False,
    only_available: bool = False,
) -> list[Candidate]:
    """Активные волонтёры для смены: с навыками, районом и доступностью (ФТ-С-03).

    Уже записанные на смену и занятые в это время другим назначением не предлагаются.
    Порядок: все навыки → доступен в это время → тот же район → ФИО.
    """
    required = list(shift.skills.values_list("pk", flat=True))
    busy = (
        Assignment.objects.active()
        .with_times()
        .filter(
            volunteer=OuterRef("pk"),
            status__in=OCCUPYING_STATUSES,
            starts__lt=shift.ends_at,
            ends__gt=shift.starts_at,
        )
    )
    queryset: QuerySet[Any] = (
        Volunteer.objects.filter(status=VolunteerStatus.ACTIVE, is_archived=False)
        .exclude(Exists(busy))
        .exclude(
            pk__in=Assignment.objects.filter(
                shift=shift, status__in=OCCUPYING_STATUSES, is_archived=False
            ).values("volunteer")
        )
        .annotate(
            matched=(
                Count("skills", filter=Q(skills__in=required), distinct=True)
                if required
                else Value(0, output_field=IntegerField())
            ),
            is_available=Exists(
                VolunteerAvailability.objects.filter(_availability_condition(shift))
            ),
        )
        .select_related("district")
        .prefetch_related("skills")
    )
    query = normalize_name(query)
    if query:
        queryset = queryset.filter(search_name__contains=query)
    if only_skills and required:
        queryset = queryset.filter(matched=len(required))
    if only_district and shift.district_id is not None:
        queryset = queryset.filter(district_id=shift.district_id)
    if only_available:
        queryset = queryset.filter(is_available=True)

    candidates = [
        Candidate(
            volunteer=v,
            has_skills=v.matched == len(required),
            same_district=shift.district_id is not None and v.district_id == shift.district_id,
            available=v.is_available,
        )
        for v in queryset.order_by("last_name", "first_name")[: CANDIDATES_LIMIT * 3]
    ]
    candidates.sort(key=lambda c: -c.score)  # сортировка устойчивая: внутри — по ФИО
    return candidates[:CANDIDATES_LIMIT]


# ---------------------------------------------------------------------------
# Смены и визиты в карточке волонтёра (ФТ-С-04: неявки — в статистике волонтёра)
# ---------------------------------------------------------------------------
@dataclass
class VolunteerShifts:
    upcoming: list[Assignment]
    recent: list[Assignment]
    attended: int
    no_show: int
    declined: int
    hours: Decimal
    last_shift_at: datetime.datetime | None
    can_manage: bool
    bot_linked: bool
    bot_code_pending: bool
    bot_username: str = ""


def volunteer_shifts(user: Any, volunteer: Volunteer) -> VolunteerShifts | None:
    """Только для ролей с полной карточкой волонтёра (координатор, администратор)."""
    if not permissions.can_manage(user):
        return None
    mine = (
        Assignment.objects.visible_to(user)
        .active()
        .filter(volunteer=volunteer)
        .with_times()
        .select_related("shift", "beneficiary")
    )
    now = timezone.now()
    counts = mine.aggregate(
        attended=Count("pk", filter=Q(status=AssignmentStatus.ATTENDED)),
        no_show=Count("pk", filter=Q(status=AssignmentStatus.NO_SHOW)),
        declined=Count("pk", filter=Q(status=AssignmentStatus.DECLINED)),
    )
    last = (
        mine.filter(kind=AssignmentKind.SHIFT, status=AssignmentStatus.ATTENDED)
        .order_by("-starts")
        .values_list("starts", flat=True)
        .first()
    )
    return VolunteerShifts(
        upcoming=list(
            mine.filter(ends__gt=now, status__in=PENDING_STATUSES).order_by("starts")[:10]
        ),
        recent=list(mine.filter(ends__lte=now).order_by("-starts")[:10]),
        attended=counts["attended"],
        no_show=counts["no_show"],
        declined=counts["declined"],
        hours=shift_hours_total(volunteer),
        last_shift_at=last,
        can_manage=not volunteer.is_archived,
        bot_linked=volunteer.telegram_id is not None,
        bot_code_pending=bool(
            volunteer.bot_link_code
            and volunteer.bot_link_code_expires
            and volunteer.bot_link_code_expires > now
        ),
        bot_username=settings.TELEGRAM_BOT_USERNAME,
    )


# ---------------------------------------------------------------------------
# Визиты в карточке подопечного (ФТ-С-05)
# ---------------------------------------------------------------------------
@dataclass
class BeneficiaryVisits:
    upcoming: list[Assignment]
    recent: list[Assignment]
    can_assign: bool


def beneficiary_visits(user: Any, beneficiary: Beneficiary) -> BeneficiaryVisits | None:
    if not permissions.can_view(user):
        return None
    visits = (
        Assignment.objects.visible_to(user)
        .active()
        .filter(beneficiary=beneficiary)
        .with_times()
        .select_related("volunteer")
    )
    now = timezone.now()
    return BeneficiaryVisits(
        upcoming=list(
            visits.filter(ends__gt=now, status__in=PENDING_STATUSES).order_by("starts")[:10]
        ),
        recent=list(visits.filter(ends__lte=now).order_by("-starts")[:5]),
        can_assign=permissions.can_manage(user)
        and not beneficiary.is_archived
        and beneficiary.status == CaseStatus.IN_WORK,
    )


def get_visit(user: Any, pk: Any) -> Assignment:
    if not permissions.can_view(user):
        raise PermissionDenied
    queryset = Assignment.objects.visible_to(user).select_related(
        "volunteer", "beneficiary", "program", "created_by"
    )
    return get_object_or_404(queryset, pk=pk, kind=AssignmentKind.VISIT)
