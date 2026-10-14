"""Расчёт отчётов ФТ-О-01…03, ФТ-О-05. Правила подсчёта описаны в docs/reports.md — по ним фонд
сверяет отчёт с ручным расчётом.

Обезличенные карточки (ФТ-А-04) учитываются: их факты помощи, часы и статусы остаются
в статистике. Карточки, ушедшие в архив как дубли или ошибки, — нет.
"""

from __future__ import annotations

import datetime
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.db.models import Count, OuterRef, Q, QuerySet, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.beneficiaries.models import Beneficiary
from apps.core.models import Program
from apps.requests.models import OPEN_STATUSES, Assistance, HelpRequest, RequestStatus, Urgency
from apps.shifts.models import Assignment, AssignmentKind, AssignmentStatus, Shift
from apps.volunteers.models import (
    Volunteer,
    VolunteerHours,
    VolunteerStatus,
    VolunteerStatusChange,
)

from . import permissions

ZERO = Decimal("0.00")
WITHOUT_PROGRAM = "none"
VS = VolunteerStatus
FUNNEL_STEPS = (VS.APPLICATION, VS.INTERVIEW, VS.TRAINING, VS.ACTIVE)


@dataclass(frozen=True)
class Period:
    date_from: datetime.date
    date_to: datetime.date

    @property
    def start(self) -> datetime.datetime:
        """Начало первого дня — во времени фонда."""
        return timezone.make_aware(datetime.datetime.combine(self.date_from, datetime.time.min))

    @property
    def end(self) -> datetime.datetime:
        """Начало дня после последнего (граница не включается)."""
        next_day = self.date_to + datetime.timedelta(days=1)
        return timezone.make_aware(datetime.datetime.combine(next_day, datetime.time.min))

    @property
    def label(self) -> str:
        return f"{self.date_from:%d.%m.%Y} — {self.date_to:%d.%m.%Y}"


def previous_quarter(today: datetime.date | None = None) -> Period:
    """Прошлый календарный квартал — период отчёта по умолчанию (сценарий 9.2)."""
    today = today or timezone.localdate()
    quarter_start = datetime.date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    end = quarter_start - datetime.timedelta(days=1)
    start = datetime.date(end.year, 3 * ((end.month - 1) // 3) + 1, 1)
    return Period(start, end)


def _counted_volunteers() -> QuerySet[Volunteer]:
    """Волонтёры для статистики: действующие карточки и обезличенные."""
    return Volunteer.objects.filter(Q(is_archived=False) | Q(anonymized_at__isnull=False))


def _program_filter(prefix: str, program: Program | str | None) -> Q:
    if program is None:
        return Q()
    if program == WITHOUT_PROGRAM:
        return Q(**{f"{prefix}__isnull": True})
    return Q(**{prefix: program})


def program_label(program: Program | str | None) -> str:
    if program is None:
        return "Все программы"
    if program == WITHOUT_PROGRAM:
        return "Без программы"
    return str(program)


# ---------------------------------------------------------------------------
# ФТ-О-01: отчёт по программе
# ---------------------------------------------------------------------------
@dataclass
class TypeRow:
    name: str
    unit: str
    facts: int
    quantity: Decimal | None
    amount: Decimal


@dataclass
class ProgramReport:
    period: Period
    program: str
    scope: str
    beneficiaries: int
    facts: int
    amount: Decimal
    by_type: list[TypeRow]
    by_category: list[tuple[str, int]]
    # Часы волонтёров по программе — только при отчёте по всем подопечным
    volunteer_hours: Decimal | None = None
    volunteers: int | None = None


def _assistance_scope(user: Any) -> QuerySet[Assistance]:
    queryset = Assistance.objects.filter(is_archived=False)
    if permissions.program_report_all(user):
        return queryset
    # Куратор — по своим подопечным (матрица 2.2, «Отчёты: П по своим»)
    return queryset.filter(beneficiary__in=Beneficiary.objects.owned_by(user))


def program_report(user: Any, period: Period, program: Program | str | None) -> ProgramReport:
    facts = _assistance_scope(user).filter(
        provided_on__gte=period.date_from,
        provided_on__lte=period.date_to,
    )
    facts = facts.filter(_program_filter("program", program))
    totals = facts.aggregate(n=Count("pk"), amount=Sum("amount"))
    by_type = [
        TypeRow(
            name=row["assistance_type__name"],
            unit=row["unit"],
            facts=row["facts"],
            quantity=row["quantity"],
            amount=row["amount"] or ZERO,
        )
        for row in facts.values("assistance_type__name", "unit")
        .annotate(facts=Count("pk"), quantity=Sum("quantity"), amount=Sum("amount"))
        .order_by("assistance_type__name", "unit")
    ]
    by_category = [
        (row["beneficiary__category__name"] or "Без категории", row["n"])
        for row in facts.values("beneficiary__category__name")
        .annotate(n=Count("beneficiary", distinct=True))
        .order_by("beneficiary__category__name")
    ]
    full = permissions.program_report_all(user)
    report = ProgramReport(
        period=period,
        program=program_label(program),
        scope="все подопечные" if full else "мои подопечные",
        beneficiaries=facts.values("beneficiary").distinct().count(),
        facts=totals["n"],
        amount=totals["amount"] or ZERO,
        by_type=by_type,
        by_category=by_category,
    )
    if full:
        hours, volunteers = _program_hours(period, program)
        report.volunteer_hours, report.volunteers = hours, volunteers
    return report


def _attended(period: Period) -> Any:
    """Отметки «Пришёл»: смены и визиты, начавшиеся в периоде."""
    return (
        Assignment.objects.filter(status=AssignmentStatus.ATTENDED, is_archived=False)
        .with_times()
        .filter(starts__gte=period.start, starts__lt=period.end)
    )


def _program_hours(period: Period, program: Program | str | None) -> tuple[Decimal, int]:
    assignments = _attended(period).filter(
        _program_filter("shift__program", program) & Q(kind=AssignmentKind.SHIFT)
        | _program_filter("program", program) & Q(kind=AssignmentKind.VISIT)
    )
    manual = VolunteerHours.objects.filter(
        is_archived=False, worked_on__gte=period.date_from, worked_on__lte=period.date_to
    ).filter(_program_filter("program", program))
    hours = (assignments.aggregate(h=Sum("hours"))["h"] or ZERO) + (
        manual.aggregate(h=Sum("hours"))["h"] or ZERO
    )
    people = set(assignments.values_list("volunteer", flat=True)) | set(
        manual.values_list("volunteer", flat=True)
    )
    return hours, len(people)


# ---------------------------------------------------------------------------
# ФТ-О-02: отчёт по волонтёрам
# ---------------------------------------------------------------------------
@dataclass
class VolunteerRow:
    name: str
    shift_hours: Decimal = ZERO
    visit_hours: Decimal = ZERO
    manual_hours: Decimal = ZERO
    attended: int = 0
    no_show: int = 0

    @property
    def total(self) -> Decimal:
        return self.shift_hours + self.visit_hours + self.manual_hours


@dataclass
class VolunteerReport:
    period: Period
    active_at_start: int
    active_at_end: int
    new_applications: int
    newly_active: int
    left: int
    shifts_held: int
    shift_hours: Decimal
    visit_hours: Decimal
    manual_hours: Decimal
    attended: int
    no_show: int
    working_volunteers: int
    by_program: list[tuple[str, Decimal]]
    rows: list[VolunteerRow] = field(default_factory=list)
    with_names: bool = False

    @property
    def total_hours(self) -> Decimal:
        return self.shift_hours + self.visit_hours + self.manual_hours

    @property
    def no_show_rate(self) -> Decimal | None:
        marked = self.attended + self.no_show
        if not marked:
            return None
        return (Decimal(self.no_show) * 100 / marked).quantize(Decimal("0.1"))


def status_on(moment: datetime.datetime) -> Subquery:
    """Статус волонтёра на момент времени — по журналу смены статусов."""
    return Subquery(
        VolunteerStatusChange.objects.filter(volunteer=OuterRef("pk"), created_at__lt=moment)
        .order_by("-created_at")
        .values("to_status")[:1]
    )


def _active_on(moment: datetime.datetime) -> int:
    return (
        _counted_volunteers()
        .exclude(anonymized_at__lt=moment)
        .annotate(status_then=status_on(moment))
        .filter(status_then=VS.ACTIVE)
        .count()
    )


def volunteer_report(user: Any, period: Period) -> VolunteerReport:
    volunteers = _counted_volunteers()
    attended = _attended(period)
    marked = (
        Assignment.objects.filter(
            status__in=[AssignmentStatus.ATTENDED, AssignmentStatus.NO_SHOW], is_archived=False
        )
        .with_times()
        .filter(starts__gte=period.start, starts__lt=period.end)
    )
    manual = VolunteerHours.objects.filter(
        is_archived=False, worked_on__gte=period.date_from, worked_on__lte=period.date_to
    )
    by_kind = {
        row["kind"]: row["h"] or ZERO
        for row in attended.values("kind").annotate(h=Sum("hours")).order_by()
    }
    marks = marked.aggregate(
        attended=Count("pk", filter=Q(status=AssignmentStatus.ATTENDED)),
        no_show=Count("pk", filter=Q(status=AssignmentStatus.NO_SHOW)),
    )

    programs: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for row in (
        attended.annotate(program_name=Coalesce("shift__program__name", "program__name"))
        .values("program_name")
        .annotate(h=Sum("hours"))
        .order_by()
    ):
        programs[row["program_name"] or "Без программы"] += row["h"] or ZERO
    for row in manual.values("program__name").annotate(h=Sum("hours")).order_by():
        programs[row["program__name"] or "Без программы"] += row["h"] or ZERO

    working = set(attended.values_list("volunteer", flat=True)) | set(
        manual.values_list("volunteer", flat=True)
    )
    report = VolunteerReport(
        period=period,
        active_at_start=_active_on(period.start),
        active_at_end=_active_on(period.end),
        new_applications=volunteers.filter(
            created_at__gte=period.start, created_at__lt=period.end
        ).count(),
        newly_active=volunteers.filter(
            start_date__gte=period.date_from, start_date__lte=period.date_to
        ).count(),
        left=volunteers.filter(left_on__gte=period.date_from, left_on__lte=period.date_to).count(),
        shifts_held=Shift.objects.planned()
        .filter(starts_at__gte=period.start, starts_at__lt=period.end)
        .count(),
        shift_hours=by_kind.get(AssignmentKind.SHIFT, ZERO),
        visit_hours=by_kind.get(AssignmentKind.VISIT, ZERO),
        manual_hours=manual.aggregate(h=Sum("hours"))["h"] or ZERO,
        attended=marks["attended"],
        no_show=marks["no_show"],
        working_volunteers=len(working),
        by_program=sorted(programs.items()),
    )
    if permissions.can_see_names(user):
        report.with_names = True
        report.rows = _volunteer_rows(marked, manual)
    return report


def _volunteer_rows(marked: Any, manual: QuerySet[VolunteerHours]) -> list[VolunteerRow]:
    rows: dict[Any, VolunteerRow] = {}
    names = {}

    def row(pk: Any) -> VolunteerRow:
        if pk not in rows:
            rows[pk] = VolunteerRow(name="")
        return rows[pk]

    for item in (
        marked.values("volunteer", "kind", "status")
        .annotate(n=Count("pk"), h=Sum("hours"))
        .order_by()
    ):
        r = row(item["volunteer"])
        if item["status"] == AssignmentStatus.ATTENDED:
            r.attended += item["n"]
            if item["kind"] == AssignmentKind.SHIFT:
                r.shift_hours += item["h"] or ZERO
            else:
                r.visit_hours += item["h"] or ZERO
        else:
            r.no_show += item["n"]
    for item in manual.values("volunteer").annotate(h=Sum("hours")).order_by():
        row(item["volunteer"]).manual_hours += item["h"] or ZERO
    for volunteer in Volunteer.objects.filter(pk__in=list(rows)).only(
        "last_name", "first_name", "middle_name"
    ):
        names[volunteer.pk] = volunteer.full_name
    for pk, r in rows.items():
        r.name = names.get(pk, "")
    return sorted(rows.values(), key=lambda r: (-r.total, r.name))


# ---------------------------------------------------------------------------
# ФТ-О-03: воронка волонтёров
# ---------------------------------------------------------------------------
@dataclass
class FunnelStep:
    status: str
    label: str
    reached: int
    from_previous: Decimal | None
    from_start: Decimal | None


@dataclass
class FunnelReport:
    period: Period
    cohort: int
    steps: list[FunnelStep]
    left_before_active: int
    average_days_to_active: Decimal | None
    current: list[tuple[str, int]]


def _percent(part: int, whole: int) -> Decimal | None:
    if not whole:
        return None
    return (Decimal(part) * 100 / whole).quantize(Decimal("0.1"))


def funnel_report(user: Any, period: Period) -> FunnelReport:
    """Когорта — анкеты, поступившие в периоде. Шаг «достигнут», если волонтёр хоть раз
    был переведён в этот статус (по журналу статусов), даже если потом выбыл."""
    cohort = _counted_volunteers().filter(created_at__gte=period.start, created_at__lt=period.end)
    total = cohort.count()
    steps: list[FunnelStep] = []
    previous = total
    for status in FUNNEL_STEPS:
        reached = (
            total
            if status == VS.APPLICATION
            else cohort.filter(status_changes__to_status=status).distinct().count()
        )
        steps.append(
            FunnelStep(
                status=status,
                label=VS(status).label,
                reached=reached,
                from_previous=_percent(reached, previous),
                from_start=_percent(reached, total),
            )
        )
        previous = reached
    durations = [
        (start - timezone.localtime(created).date()).days
        for created, start in cohort.filter(start_date__isnull=False).values_list(
            "created_at", "start_date"
        )
        if start is not None
    ]
    average = (
        (Decimal(sum(durations)) / len(durations)).quantize(Decimal("0.1")) if durations else None
    )
    counts = dict(
        Volunteer.objects.filter(is_archived=False)
        .values("status")
        .annotate(n=Count("pk"))
        .order_by()
        .values_list("status", "n")
    )
    return FunnelReport(
        period=period,
        cohort=total,
        steps=steps,
        left_before_active=cohort.filter(status=VS.LEFT, start_date__isnull=True).count(),
        average_days_to_active=average,
        current=[(label, counts.get(value, 0)) for value, label in VS.choices],
    )


# ---------------------------------------------------------------------------
# ФТ-О-05: отчёт по заявкам
# ---------------------------------------------------------------------------
@dataclass
class RequestTypeRow:
    name: str
    received: int
    done: int
    average_days: Decimal | None
    open_at_end: int
    overdue_at_end: int


@dataclass
class RequestReport:
    period: Period
    scope: str
    state_label: str  # на какой момент «открытые» и «просроченные»
    received: int
    done: int
    rejected: int
    cancelled: int
    average_days: Decimal | None
    median_days: Decimal | None
    open_at_end: int
    overdue_at_end: int
    by_type: list[RequestTypeRow]
    open_by_urgency: list[tuple[str, int]]


def _request_scope(user: Any) -> QuerySet[HelpRequest]:
    queryset = HelpRequest.objects.filter(is_archived=False)
    if permissions.program_report_all(user):
        return queryset
    return queryset.filter(beneficiary__in=Beneficiary.objects.owned_by(user))


def _days(values: list[datetime.timedelta]) -> tuple[Decimal | None, Decimal | None]:
    """Среднее и медиана в днях, с одним знаком."""
    if not values:
        return None, None
    days = [v.total_seconds() / 86400 for v in values]
    step = Decimal("0.1")
    return (
        Decimal(str(sum(days) / len(days))).quantize(step),
        Decimal(str(statistics.median(days))).quantize(step),
    )


def request_report(user: Any, period: Period) -> RequestReport:
    """Поступившие, выполненные, отклонённые и отменённые за период; среднее время от
    поступления до выполнения; открытые и просроченные на конец периода (для текущего
    периода — на сейчас). Конечные статусы не меняются (п. 4.3), поэтому заявка была
    открыта в момент T, если поступила раньше T и закрыта не раньше T."""
    scope = _request_scope(user)
    today = timezone.localdate()
    if period.date_to >= today:
        moment, overdue_before, state_label = timezone.now(), today, "сейчас"
    else:
        moment = period.end
        overdue_before = period.date_to + datetime.timedelta(days=1)
        state_label = f"на конец {period.date_to:%d.%m.%Y}"

    received = scope.filter(created_at__gte=period.start, created_at__lt=period.end)
    closed_in_period = scope.filter(
        status_changed_at__gte=period.start, status_changed_at__lt=period.end
    )
    done = scope.filter(
        status=RequestStatus.DONE,
        completed_at__gte=period.start,
        completed_at__lt=period.end,
    )
    open_at_end = scope.filter(created_at__lt=moment).filter(
        Q(status__in=OPEN_STATUSES) | Q(status_changed_at__gte=moment)
    )
    overdue_at_end = open_at_end.filter(due_date__lt=overdue_before)

    durations = [
        completed - created
        for created, completed in done.values_list("created_at", "completed_at")
        if completed is not None
    ]
    average, median = _days(durations)

    def counts(queryset: QuerySet[HelpRequest]) -> dict[str, int]:
        return dict(
            queryset.values("assistance_type__name")
            .annotate(n=Count("pk"))
            .order_by()
            .values_list("assistance_type__name", "n")
        )

    durations_by_type: dict[str, list[datetime.timedelta]] = defaultdict(list)
    for name, created, completed in done.values_list(
        "assistance_type__name", "created_at", "completed_at"
    ):
        if completed is not None:
            durations_by_type[name].append(completed - created)
    received_n, done_n = counts(received), counts(done)
    open_n, overdue_n = counts(open_at_end), counts(overdue_at_end)
    names = sorted(set(received_n) | set(done_n) | set(open_n))
    by_type = [
        RequestTypeRow(
            name=name,
            received=received_n.get(name, 0),
            done=done_n.get(name, 0),
            average_days=_days(durations_by_type[name])[0],
            open_at_end=open_n.get(name, 0),
            overdue_at_end=overdue_n.get(name, 0),
        )
        for name in names
    ]
    urgency = dict(
        open_at_end.values("urgency").annotate(n=Count("pk")).order_by().values_list("urgency", "n")
    )
    return RequestReport(
        period=period,
        scope="все подопечные" if permissions.program_report_all(user) else "мои подопечные",
        state_label=state_label,
        received=received.count(),
        done=len(durations),
        rejected=closed_in_period.filter(status=RequestStatus.REJECTED).count(),
        cancelled=closed_in_period.filter(status=RequestStatus.CANCELLED).count(),
        average_days=average,
        median_days=median,
        open_at_end=open_at_end.count(),
        overdue_at_end=overdue_at_end.count(),
        by_type=by_type,
        open_by_urgency=[(label, urgency.get(value, 0)) for value, label in Urgency.choices],
    )
