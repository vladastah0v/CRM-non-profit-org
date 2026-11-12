"""Выгрузка списков в Excel с фильтрами и в пределах прав пользователя (ФТ-О-04, п. 7.3).

Выборка — та же, что на экране списка (visible_to + фильтры). Роли с ограниченным
доступом получают только поля набора «О»; защищённые данные подопечного (здоровье,
документы) и тексты заявок не выгружаются никогда. Каждая выгрузка — в журнал аудита:
число строк и структурные фильтры, без текста поиска (в нём бывают ФИО и телефоны).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db.models import DecimalField, OuterRef, QuerySet, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpRequest, QueryDict

from apps.accounts.roles import RoleCode, user_has_role
from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.beneficiaries import filters as beneficiary_filters
from apps.beneficiaries import permissions as beneficiary_permissions
from apps.beneficiaries import selectors as beneficiary_selectors
from apps.beneficiaries.models import Beneficiary
from apps.core.normalize import format_phone
from apps.core.xlsx import Sheet
from apps.requests import filters as request_filters
from apps.requests import permissions as request_permissions
from apps.requests.models import HelpRequest
from apps.shifts import filters as shift_filters
from apps.shifts import permissions as shift_permissions
from apps.shifts.models import Assignment, AssignmentStatus, Shift
from apps.volunteers import filters as volunteer_filters
from apps.volunteers import permissions as volunteer_permissions
from apps.volunteers import selectors as volunteer_selectors
from apps.volunteers.models import VolunteerHours

MAX_ROWS = 10_000
HOURS = DecimalField(max_digits=10, decimal_places=2)


@dataclass
class ListExport:
    title: str
    filename: str
    header: Sequence[str]
    rows: Iterable[Sequence[Any]]
    count: int
    saved_params: Sequence[str]


def _names(items: Iterable[Any]) -> str:
    return ", ".join(str(item) for item in items)


def _hours_subquery(model: Any, **filters: Any) -> Coalesce:
    total = (
        model.objects.filter(volunteer=OuterRef("pk"), is_archived=False, **filters)
        .values("volunteer")
        .annotate(total=Sum("hours"))
        .values("total")
    )
    return Coalesce(Subquery(total, output_field=HOURS), Value(Decimal(0)), output_field=HOURS)


def _limit(queryset: QuerySet) -> tuple[Iterable[Any], int]:
    count = queryset.count()
    return queryset[:MAX_ROWS].iterator(chunk_size=200), count


# ---------------------------------------------------------------------------
# Списки
# ---------------------------------------------------------------------------
def volunteers(user: Any, params: QueryDict) -> ListExport:
    if not volunteer_permissions.can_view(user):
        raise PermissionDenied
    full = volunteer_permissions.sees_full_card(user)
    queryset = volunteer_selectors.volunteer_list(user, params).qs
    if full:
        queryset = queryset.select_related("source").annotate(
            manual_total=_hours_subquery(VolunteerHours),
            shift_total=_hours_subquery(Assignment, status=AssignmentStatus.ATTENDED),
        )
        header = [
            "Фамилия", "Имя", "Отчество", "Дата рождения", "Телефон", "Email", "Telegram",
            "Город", "Район", "Навыки", "Программы", "Источник", "Статус", "Дата начала",
            "Отработано часов", "Telegram-бот подключён",
        ]  # fmt: skip
    else:
        header = ["Фамилия", "Имя", "Отчество", "Телефон", "Telegram", "Район", "Навыки", "Статус"]
    items, count = _limit(queryset)

    def row(v: Any) -> list[Any]:
        if not full:
            return [
                v.last_name, v.first_name, v.middle_name, format_phone(v.phone),
                v.telegram_username, v.district, _names(v.skills.all()), v.get_status_display(),
            ]  # fmt: skip
        return [
            v.last_name, v.first_name, v.middle_name, v.birth_date, format_phone(v.phone),
            v.email, v.telegram_username, v.city, v.district, _names(v.skills.all()),
            _names(v.programs.all()), v.source, v.get_status_display(), v.start_date,
            v.manual_total + v.shift_total, v.telegram_id is not None,
        ]  # fmt: skip

    return ListExport(
        "Волонтёры",
        "volontery",
        header,
        (row(v) for v in items),
        count,
        volunteer_filters.SAVED_PARAMS,
    )


def beneficiaries(user: Any, params: QueryDict) -> ListExport:
    """Полные поля — администратору и куратору по своим подопечным. Остальные строки
    (координатору — все) — только набор «О», даже если у пользователя обе роли."""
    if not beneficiary_permissions.can_view_list(user):
        raise PermissionDenied
    is_admin = beneficiary_permissions.is_admin(user)
    full_columns = is_admin or user_has_role(user, RoleCode.CURATOR)
    queryset = beneficiary_selectors.beneficiary_list(user, params).qs
    owned: set[Any] = set()
    if full_columns:
        header = [
            "Фамилия", "Имя", "Отчество", "Дата рождения", "Адрес", "Телефон", "Категория",
            "Источник обращения", "Куратор", "Программы", "Статус случая", "Дата закрытия",
            "Причина закрытия",
        ]  # fmt: skip
        if not is_admin:
            owned = set(Beneficiary.objects.owned_by(user).values_list("pk", flat=True))
    else:
        # Набор «О» координатора: без даты рождения, программ и истории
        header = [
            "Фамилия",
            "Имя",
            "Отчество",
            "Телефон",
            "Адрес",
            "Категория",
            "Статус",
            "Куратор",
        ]
    items, count = _limit(queryset)

    def row(b: Any) -> list[Any]:
        if not full_columns:
            return [
                b.last_name, b.first_name, b.middle_name, format_phone(b.phone), b.address,
                b.category, b.get_status_display(), b.curator,
            ]  # fmt: skip
        if not is_admin and b.pk not in owned:
            # Чужая карточка в выборке координатора: только поля набора «О»
            return [
                b.last_name, b.first_name, b.middle_name, "", b.address, format_phone(b.phone),
                b.category, "", b.curator, "", b.get_status_display(), "", "",
            ]  # fmt: skip
        return [
            b.last_name, b.first_name, b.middle_name, b.birth_date, b.address,
            format_phone(b.phone), b.category, b.source, b.curator, _names(b.programs.all()),
            b.get_status_display(), b.closed_on, b.closing_reason,
        ]  # fmt: skip

    return ListExport(
        "Подопечные",
        "podopechnye",
        header,
        (row(b) for b in items),
        count,
        beneficiary_filters.SAVED_PARAMS,
    )


def requests(user: Any, params: QueryDict) -> ListExport:
    if not request_permissions.can_view(user):
        raise PermissionDenied
    visible = HelpRequest.objects.visible_to(user).select_related(
        "beneficiary", "assistance_type", "responsible"
    )
    queryset = request_filters.RequestFilter(params, queryset=visible, user=user).qs
    header = [
        "Подопечный", "Тип помощи", "Срочность", "Статус", "Желаемая дата", "Просрочена",
        "Ответственный", "Создана", "Выполнена",
    ]  # fmt: skip
    items, count = _limit(queryset)
    rows = (
        [
            r.beneficiary.full_name,
            r.assistance_type,
            r.get_urgency_display(),
            r.get_status_display(),
            r.due_date,
            r.is_overdue,
            r.responsible,
            r.created_at,
            r.completed_at,
        ]
        for r in items
    )
    return ListExport("Заявки", "zayavki", header, rows, count, request_filters.SAVED_PARAMS)


def shifts(user: Any, params: QueryDict) -> ListExport:
    if not shift_permissions.can_view(user):
        raise PermissionDenied
    queryset = shift_filters.with_occupied(
        Shift.objects.visible_to(user).select_related("shift_type", "district", "program")
    )
    queryset = shift_filters.ShiftFilter(params, queryset=queryset).qs
    header = [
        "Название", "Тип", "Начало", "Окончание", "Место", "Район", "Программа",
        "Нужно волонтёров", "Записано", "Статус",
    ]  # fmt: skip
    items, count = _limit(queryset)
    rows = (
        [
            s.title,
            s.shift_type,
            s.starts_at,
            s.ends_at,
            s.address,
            s.district,
            s.program,
            s.volunteers_needed,
            s.occupied,
            s.state_display,
        ]
        for s in items
    )
    return ListExport("Смены", "smeny", header, rows, count, shift_filters.SAVED_PARAMS)


EXPORTS: dict[str, Callable[[Any, QueryDict], ListExport]] = {
    "volunteers": volunteers,
    "beneficiaries": beneficiaries,
    "requests": requests,
    "shifts": shifts,
}


def build(kind: str, request: HttpRequest) -> tuple[ListExport, Sheet]:
    export = EXPORTS[kind](request.user, request.GET)
    caption = [f"{export.title}: выгрузка с фильтрами списка"]
    if export.count > MAX_ROWS:
        caption.append(f"Найдено {export.count}, выгружены первые {MAX_ROWS} — уточните фильтры.")
    log_event(
        AuditAction.EXPORT,
        request=request,
        object_type=f"{kind}.list",
        extra={
            "rows": min(export.count, MAX_ROWS),
            "filters": {
                key: request.GET.getlist(key) for key in export.saved_params if key in request.GET
            },
            "search": bool(request.GET.get("q", "").strip()),
        },
    )
    return export, Sheet(export.title, export.header, export.rows, caption=caption)
