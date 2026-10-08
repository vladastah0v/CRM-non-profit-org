"""Внутренний API Telegram-бота волонтёров: /api/v1/bot/ (п. 7.2, 7.5 ТЗ).

- Бот — отдельный процесс без доступа к базе; ходит сюда с сервисным токеном
  (`Authorization: Bearer …`, право «bot») по внутренней сети Docker. Снаружи Nginx
  этот путь не пропускает.
- Волонтёр определяется по заголовку `X-Telegram-User-Id`. Все выборки — через
  visible_to(учётная запись волонтёра): бот видит только свои назначения волонтёра.
- Данные подопечного (имя, адрес, телефон, комментарий) — только в деталях визита и
  только пока визит предстоит; каждый показ пишется в журнал аудита.
- Ошибки правил — 400 {"detail": "текст для волонтёра"}.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import Count, Exists, F, OuterRef, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import exceptions, serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.accounts.models import ServiceToken, TokenScope, User
from apps.accounts.tokens import HasTokenScope, ServiceTokenAuthentication
from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.beneficiaries.models import CaseStatus
from apps.core.context import acting_as
from apps.volunteers import services as volunteer_services
from apps.volunteers.models import Volunteer, VolunteerStatus

from . import services
from .models import (
    OCCUPYING_STATUSES,
    PENDING_STATUSES,
    Assignment,
    AssignmentStatus,
    Shift,
)
from .services import ATTENDANCE_WINDOW

TELEGRAM_HEADER = "X-Telegram-User-Id"
# Детали визита доступны ещё 2 часа после окончания — на случай задержки
VISIT_DETAILS_GRACE = datetime.timedelta(hours=2)
OPEN_SHIFTS_DAYS = 30
OPEN_SHIFTS_LIMIT = 20

TELEGRAM_PARAMETER = OpenApiParameter(
    TELEGRAM_HEADER, int, OpenApiParameter.HEADER, required=True, description="Telegram ID"
)


class NotLinked(exceptions.PermissionDenied):
    """403 с кодом: бот отличает «не привязан» от неверного токена."""

    default_detail = "Telegram не привязан к карточке волонтёра."
    default_code = "not_linked"

    def __init__(self) -> None:
        super().__init__({"detail": self.default_detail, "code": self.default_code})


class TokenThrottle(SimpleRateThrottle):
    """Лимит на сервисный токен, а не на IP: все запросы бота идут с одного адреса."""

    scope = "bot"

    def get_cache_key(self, request: Any, view: Any) -> str:
        token = request.auth
        ident = str(token.pk) if isinstance(token, ServiceToken) else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class LinkThrottle(SimpleRateThrottle):
    """Попытки ввода кода привязки — на каждый Telegram ID (подбор кода)."""

    scope = "bot_link"

    def get_cache_key(self, request: Any, view: Any) -> str:
        return self.cache_format % {"scope": self.scope, "ident": _telegram_id(request)}


def _telegram_id(request: Request) -> int:
    raw = request.headers.get(TELEGRAM_HEADER, "")
    try:
        value = int(raw)
    except ValueError as exc:
        raise exceptions.ValidationError({"detail": f"Нужен заголовок {TELEGRAM_HEADER}."}) from exc
    if value <= 0:
        raise exceptions.ValidationError({"detail": f"Неверный {TELEGRAM_HEADER}."})
    return value


def _user(volunteer: Volunteer) -> User:
    """Учётная запись волонтёра — для visible_to и подписи изменений."""
    if volunteer.user is None:  # уже проверено в BotView.volunteer
        raise NotLinked
    return volunteer.user


def _rule_error(exc: ValidationError) -> Response:
    return Response({"detail": " ".join(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)


class BotView(APIView):
    authentication_classes = [ServiceTokenAuthentication]
    permission_classes = [HasTokenScope]
    required_scope = TokenScope.BOT
    throttle_classes = [TokenThrottle]

    def volunteer(self, request: Request) -> Volunteer:
        """Волонтёр по Telegram ID: привязан, не в архиве, не выбыл."""
        volunteer = (
            Volunteer.objects.filter(telegram_id=_telegram_id(request), is_archived=False)
            .exclude(status=VolunteerStatus.LEFT)
            .select_related("user", "district")
            .first()
        )
        if volunteer is None or volunteer.user is None or not volunteer.user.is_active:
            raise NotLinked
        return volunteer

    def assignment(self, volunteer: Volunteer, pk: Any) -> Assignment:
        queryset = Assignment.objects.visible_to(_user(volunteer)).select_related(
            "shift", "volunteer", "beneficiary"
        )
        return get_object_or_404(queryset, pk=pk)


# ---------------------------------------------------------------------------
# Представление данных для бота
# ---------------------------------------------------------------------------
def _hours(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def assignment_data(assignment: Assignment, now: datetime.datetime) -> dict[str, Any]:
    starts, ends = assignment.starts_at, assignment.ends_at
    pending = assignment.status in PENDING_STATUSES and not assignment.is_archived
    cancelled_shift = assignment.shift is not None and assignment.shift.is_cancelled
    return {
        "id": str(assignment.pk),
        "kind": assignment.kind,
        "title": assignment.title,
        "starts_at": timezone.localtime(starts).isoformat(),
        "ends_at": timezone.localtime(ends).isoformat(),
        # Адрес смены — не персональные данные; адрес подопечного — только в деталях
        "address": assignment.shift.address if assignment.shift is not None else None,
        "description": assignment.shift.description if assignment.shift is not None else "",
        "status": assignment.status,
        "status_display": assignment.get_status_display(),
        "can_confirm": pending
        and not cancelled_shift
        and assignment.status == AssignmentStatus.ASSIGNED
        and starts > now,
        "can_decline": pending and not cancelled_shift and starts > now,
        "needs_attendance": pending and not cancelled_shift and starts <= now,
        "default_hours": _hours(assignment.default_hours),
        "hours": _hours(assignment.hours),
    }


def _visit_details_allowed(visit: Assignment, now: datetime.datetime) -> bool:
    """п. 2.3: данные подопечного волонтёр получает только на время назначения."""
    beneficiary = visit.beneficiary
    return (
        visit.is_visit
        and beneficiary is not None
        and visit.status in PENDING_STATUSES
        and not visit.is_archived
        and now <= visit.ends_at + VISIT_DETAILS_GRACE
        and not beneficiary.is_archived
        and beneficiary.status == CaseStatus.IN_WORK
    )


class AttendanceSerializer(serializers.Serializer):
    attended = serializers.BooleanField()
    hours = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0.25"),
        max_value=Decimal(24),
        required=False,
        allow_null=True,
    )


class LinkSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=20)


# Описание ответов для документации OpenAPI (данные собирают функции *_data ниже)
class AssignmentOut(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=["shift", "visit"])
    title = serializers.CharField()
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    address = serializers.CharField(allow_null=True, help_text="Адрес смены; у визита — null")
    description = serializers.CharField()
    status = serializers.ChoiceField(choices=AssignmentStatus.choices)
    status_display = serializers.CharField()
    can_confirm = serializers.BooleanField()
    can_decline = serializers.BooleanField()
    needs_attendance = serializers.BooleanField()
    default_hours = serializers.DecimalField(max_digits=5, decimal_places=2)
    hours = serializers.DecimalField(max_digits=5, decimal_places=2, allow_null=True)


class VisitOut(serializers.Serializer):
    name = serializers.CharField(help_text="Только имя подопечного")
    address = serializers.CharField()
    phone = serializers.CharField()
    comment = serializers.CharField()


class AssignmentDetailOut(AssignmentOut):
    visit = VisitOut(
        allow_null=True,
        required=False,
        help_text="Только у визита и только пока он предстоит; иначе null",
    )


class AssignmentListOut(serializers.Serializer):
    results = AssignmentOut(many=True)


class ShiftOut(serializers.Serializer):
    id = serializers.UUIDField()
    title = serializers.CharField()
    type = serializers.CharField()
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    address = serializers.CharField()
    district = serializers.CharField(allow_null=True)
    description = serializers.CharField()
    free_places = serializers.IntegerField()


class ShiftListOut(serializers.Serializer):
    results = ShiftOut(many=True)
    detail = serializers.CharField(required=False)


class MeOut(serializers.Serializer):
    first_name = serializers.CharField()
    status = serializers.CharField()
    status_display = serializers.CharField()
    can_sign_up = serializers.BooleanField()
    hours_total = serializers.DecimalField(max_digits=8, decimal_places=2)
    attended_count = serializers.IntegerField()


class LinkOut(serializers.Serializer):
    first_name = serializers.CharField()


class StatusOut(serializers.Serializer):
    status = serializers.CharField()


class ErrorOut(serializers.Serializer):
    detail = serializers.CharField()
    code = serializers.CharField(required=False, help_text="not_linked — Telegram не привязан")


ERRORS = {400: ErrorOut, 403: ErrorOut, 404: ErrorOut}


# ---------------------------------------------------------------------------
# Привязка и профиль
# ---------------------------------------------------------------------------
class LinkView(BotView):
    throttle_classes = [TokenThrottle, LinkThrottle]

    @extend_schema(
        summary="Бот: привязать Telegram по коду из CRM",
        request=LinkSerializer,
        responses={200: LinkOut, **ERRORS},
        parameters=[TELEGRAM_PARAMETER],
    )
    def post(self, request: Request) -> Response:
        telegram_id = _telegram_id(request)
        serializer = LinkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            volunteer = volunteer_services.link_telegram(
                serializer.validated_data["code"], telegram_id
            )
        except ValidationError as exc:
            return _rule_error(exc)
        return Response({"first_name": volunteer.first_name})


class UnlinkView(BotView):
    @extend_schema(
        summary="Бот: волонтёр отвязывает Telegram",
        request=None,
        responses={200: StatusOut, **ERRORS},
        parameters=[TELEGRAM_PARAMETER],
    )
    def post(self, request: Request) -> Response:
        volunteer = self.volunteer(request)
        with acting_as(volunteer.user):
            volunteer_services.unlink_telegram(volunteer, by_volunteer=True)
        return Response({"status": "unlinked"})


class MeView(BotView):
    @extend_schema(
        summary="Бот: профиль волонтёра",
        responses={200: MeOut, **ERRORS},
        parameters=[TELEGRAM_PARAMETER],
    )
    def get(self, request: Request) -> Response:
        volunteer = self.volunteer(request)
        attended = volunteer.assignments.active().filter(status=AssignmentStatus.ATTENDED)
        return Response(
            {
                "first_name": volunteer.first_name,
                "status": volunteer.status,
                "status_display": volunteer.get_status_display(),
                "can_sign_up": volunteer.status == VolunteerStatus.ACTIVE,
                "hours_total": _hours(volunteer_services.total_hours(volunteer)),
                "attended_count": attended.count(),
            }
        )


# ---------------------------------------------------------------------------
# Мои смены и визиты, ответы на приглашения, явка
# ---------------------------------------------------------------------------
class MyAssignmentsView(BotView):
    @extend_schema(
        summary="Бот: мои предстоящие смены и визиты и те, где нужна отметка явки",
        operation_id="bot_assignments_list",
        responses={200: AssignmentListOut, **ERRORS},
        parameters=[TELEGRAM_PARAMETER],
    )
    def get(self, request: Request) -> Response:
        volunteer = self.volunteer(request)
        now = timezone.now()
        assignments = (
            Assignment.objects.visible_to(_user(volunteer))
            .active()
            .with_times()
            .filter(status__in=PENDING_STATUSES, ends__gt=now - ATTENDANCE_WINDOW)
            .select_related("shift", "volunteer")
            .order_by("starts")[:30]
        )
        return Response({"results": [assignment_data(a, now) for a in assignments]})


class AssignmentDetailView(BotView):
    @extend_schema(
        summary="Бот: подробности смены или визита",
        operation_id="bot_assignments_retrieve",
        responses={200: AssignmentDetailOut, **ERRORS},
        parameters=[TELEGRAM_PARAMETER],
    )
    def get(self, request: Request, pk: str) -> Response:
        volunteer = self.volunteer(request)
        assignment = self.assignment(volunteer, pk)
        now = timezone.now()
        data = assignment_data(assignment, now)
        if assignment.is_visit:
            data["visit"] = None
            beneficiary = assignment.beneficiary
            if beneficiary is not None and _visit_details_allowed(assignment, now):
                # Минимум для визита (п. 2.3, 7.2): имя, адрес, телефон, комментарий
                data["visit"] = {
                    "name": beneficiary.first_name,
                    "address": beneficiary.address,
                    "phone": beneficiary.phone,
                    "comment": assignment.task_comment,
                }
                with acting_as(volunteer.user):
                    log_event(
                        AuditAction.VIEW_SENSITIVE,
                        obj=beneficiary,
                        fields=["first_name", "address", "phone"],
                        extra={"via": "bot", "assignment": str(assignment.pk)},
                    )
        return Response(data)


class AssignmentActionView(BotView):
    action_status: str = ""

    def post(self, request: Request, pk: str) -> Response:
        volunteer = self.volunteer(request)
        assignment = self.assignment(volunteer, pk)
        try:
            with acting_as(volunteer.user):
                services.change_assignment_status(assignment, self.action_status, via_bot=True)
        except ValidationError as exc:
            return _rule_error(exc)
        return Response(assignment_data(assignment, timezone.now()))


class ConfirmView(AssignmentActionView):
    action_status = AssignmentStatus.CONFIRMED

    @extend_schema(
        summary="Бот: «Приду»",
        request=None,
        responses={200: AssignmentOut, **ERRORS},
        parameters=[TELEGRAM_PARAMETER],
    )
    def post(self, request: Request, pk: str) -> Response:
        return super().post(request, pk)


class DeclineView(AssignmentActionView):
    action_status = AssignmentStatus.DECLINED

    @extend_schema(
        summary="Бот: «Не смогу»",
        request=None,
        responses={200: AssignmentOut, **ERRORS},
        parameters=[TELEGRAM_PARAMETER],
    )
    def post(self, request: Request, pk: str) -> Response:
        return super().post(request, pk)


class AttendanceView(BotView):
    @extend_schema(
        summary="Бот: отметка явки и часов после смены",
        request=AttendanceSerializer,
        responses={200: AssignmentOut, **ERRORS},
        parameters=[TELEGRAM_PARAMETER],
    )
    def post(self, request: Request, pk: str) -> Response:
        volunteer = self.volunteer(request)
        assignment = self.assignment(volunteer, pk)
        serializer = AttendanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attended = serializer.validated_data["attended"]
        now = timezone.now()
        if assignment.ends_at <= now - ATTENDANCE_WINDOW:
            return _rule_error(
                ValidationError("Отметить явку через бота можно в течение двух суток.")
            )
        try:
            with acting_as(volunteer.user):
                services.change_assignment_status(
                    assignment,
                    AssignmentStatus.ATTENDED if attended else AssignmentStatus.NO_SHOW,
                    hours=serializer.validated_data.get("hours") if attended else None,
                    via_bot=True,
                )
        except ValidationError as exc:
            return _rule_error(exc)
        return Response(assignment_data(assignment, now))


# ---------------------------------------------------------------------------
# Запись на смену (ФТ-С-02)
# ---------------------------------------------------------------------------
def open_shifts_for(volunteer: Volunteer) -> list[Shift]:
    """Открытые смены по навыкам и району волонтёра (п. 7.2): будущие, с открытой записью
    и свободными местами; все требуемые навыки есть у волонтёра; район — его или не задан."""
    user = _user(volunteer)
    now = timezone.now()
    skills = list(volunteer.skills.values_list("pk", flat=True))
    taken = Assignment.objects.filter(
        volunteer=volunteer,
        shift__isnull=False,  # без NULL: иначе NOT IN отсечёт всё
        status__in=OCCUPYING_STATUSES,
        is_archived=False,
    ).values("shift")
    # Требуемый навык, которого у волонтёра нет
    missing_skill = Shift.skills.through.objects.filter(shift=OuterRef("pk")).exclude(
        skill__in=skills
    )
    queryset = (
        Shift.objects.visible_to(user)
        .planned()
        .filter(
            allow_signup=True,
            starts_at__gt=now,
            starts_at__lte=now + datetime.timedelta(days=OPEN_SHIFTS_DAYS),
        )
        .exclude(pk__in=taken)
        .exclude(Exists(missing_skill))
        .annotate(
            occupied=Count(
                "assignments",
                filter=Q(
                    assignments__status__in=OCCUPYING_STATUSES,
                    assignments__is_archived=False,
                ),
            ),
        )
        .filter(occupied__lt=F("volunteers_needed"))
        .select_related("shift_type", "district")
        .order_by("starts_at")
    )
    if volunteer.district_id is not None:
        queryset = queryset.filter(Q(district__isnull=True) | Q(district_id=volunteer.district_id))
    return list(queryset[:OPEN_SHIFTS_LIMIT])


def shift_data(shift: Shift) -> dict[str, Any]:
    return {
        "id": str(shift.pk),
        "title": shift.title,
        "type": str(shift.shift_type),
        "starts_at": timezone.localtime(shift.starts_at).isoformat(),
        "ends_at": timezone.localtime(shift.ends_at).isoformat(),
        "address": shift.address,
        "district": shift.district.name if shift.district is not None else None,
        "description": shift.description,
        "free_places": shift.volunteers_needed - shift.occupied,  # type: ignore[attr-defined]
    }


class OpenShiftsView(BotView):
    @extend_schema(
        summary="Бот: открытые смены для записи",
        responses={200: ShiftListOut, **ERRORS},
        parameters=[TELEGRAM_PARAMETER],
    )
    def get(self, request: Request) -> Response:
        volunteer = self.volunteer(request)
        if volunteer.status != VolunteerStatus.ACTIVE:
            return Response({"results": [], "detail": "Запись на смены — в статусе «Активен»."})
        return Response({"results": [shift_data(s) for s in open_shifts_for(volunteer)]})


class SignupView(BotView):
    @extend_schema(
        summary="Бот: записаться на смену",
        request=None,
        responses={201: AssignmentOut, **ERRORS},
        parameters=[TELEGRAM_PARAMETER],
    )
    def post(self, request: Request, pk: str) -> Response:
        volunteer = self.volunteer(request)
        shift = get_object_or_404(Shift.objects.visible_to(_user(volunteer)), pk=pk)
        try:
            with acting_as(volunteer.user):
                assignment = services.assign_to_shift(shift, volunteer, via_bot=True)
        except ValidationError as exc:
            return _rule_error(exc)
        return Response(assignment_data(assignment, timezone.now()), status=status.HTTP_201_CREATED)
