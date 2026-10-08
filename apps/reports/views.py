"""Экраны отчётов (ФТ-О-01…03) и их выгрузка в XLSX. Каждая выгрузка — в журнал аудита."""

from __future__ import annotations

from typing import Any

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.core.xlsx import Sheet, build_workbook, xlsx_response

from . import exports, permissions, services
from .forms import PeriodForm, ProgramReportForm


def _wants_xlsx(request: HttpRequest) -> bool:
    return request.GET.get("format") == "xlsx"


def _log_export(request: HttpRequest, report: str, **extra: Any) -> None:
    log_event(AuditAction.EXPORT, request=request, object_type=f"reports.{report}", extra=extra)


@login_required
@require_GET
def report_index(request: HttpRequest) -> HttpResponse:
    if not permissions.can_view_any(request.user):
        raise PermissionDenied
    return render(
        request,
        "reports/index.html",
        {
            "can_program": permissions.can_program_report(request.user),
            "program_all": permissions.program_report_all(request.user),
            "can_volunteers": permissions.can_volunteer_reports(request.user),
        },
    )


# ---------------------------------------------------------------------------
# ФТ-О-01
# ---------------------------------------------------------------------------
def program_sheets(report: services.ProgramReport) -> list[Sheet]:
    caption = [
        "Отчёт по программе",
        f"Программа: {report.program}",
        f"Период: {report.period.label}",
        f"Охват: {report.scope}",
    ]
    totals = [
        ("Уникальных подопечных", report.beneficiaries),
        ("Фактов помощи", report.facts),
        ("Сумма, ₽", report.amount),
    ]
    if report.volunteer_hours is not None:
        totals += [
            ("Часы волонтёров", report.volunteer_hours),
            ("Волонтёров участвовало", report.volunteers),
        ]
    return [
        Sheet("Итоги", ["Показатель", "Значение"], totals, caption=caption),
        Sheet(
            "По типам помощи",
            ["Тип помощи", "Единица", "Фактов", "Количество", "Сумма, ₽"],
            [(r.name, r.unit, r.facts, r.quantity, r.amount) for r in report.by_type],
            caption=caption,
        ),
        Sheet(
            "По категориям",
            ["Категория подопечных", "Уникальных подопечных"],
            report.by_category,
            caption=caption,
        ),
    ]


@login_required
@require_GET
def program_report(request: HttpRequest) -> HttpResponse:
    if not permissions.can_program_report(request.user):
        raise PermissionDenied
    form = ProgramReportForm(request.GET or None)
    report = None
    if form.is_valid():
        report = services.program_report(request.user, form.period, form.selected_program)
        if _wants_xlsx(request):
            _log_export(
                request,
                "program",
                date_from=str(form.period.date_from),
                date_to=str(form.period.date_to),
                program=str(form.cleaned_data.get("program") or "all"),
                scope="all" if permissions.program_report_all(request.user) else "own",
            )
            content = build_workbook(program_sheets(report))
            return xlsx_response(content, "otchet-po-programme")
    return render(request, "reports/program.html", {"form": form, "report": report})


# ---------------------------------------------------------------------------
# ФТ-О-02
# ---------------------------------------------------------------------------
def volunteer_sheets(report: services.VolunteerReport) -> list[Sheet]:
    caption = ["Отчёт по волонтёрам", f"Период: {report.period.label}"]
    totals = [
        ("Активных на начало периода", report.active_at_start),
        ("Активных на конец периода", report.active_at_end),
        ("Новых анкет", report.new_applications),
        ("Стали активными впервые", report.newly_active),
        ("Выбыли", report.left),
        ("Работали в периоде (были на смене, визите или внесены часы)", report.working_volunteers),
        ("Проведено смен", report.shifts_held),
        ("Отметок «пришёл»", report.attended),
        ("Неявок", report.no_show),
        ("Доля неявок, %", report.no_show_rate),
        ("Часы на сменах", report.shift_hours),
        ("Часы на визитах", report.visit_hours),
        ("Часы, внесённые вручную", report.manual_hours),
        ("Часы всего", report.total_hours),
    ]
    sheets = [
        Sheet("Итоги", ["Показатель", "Значение"], totals, caption=caption),
        Sheet("Часы по программам", ["Программа", "Часы"], report.by_program, caption=caption),
    ]
    if report.with_names:
        sheets.append(
            Sheet(
                "По волонтёрам",
                ["ФИО", "Смены, ч", "Визиты, ч", "Вручную, ч", "Всего, ч", "Пришёл", "Неявки"],
                [
                    (
                        r.name,
                        r.shift_hours,
                        r.visit_hours,
                        r.manual_hours,
                        r.total,
                        r.attended,
                        r.no_show,
                    )
                    for r in report.rows
                ],
                caption=caption,
            )
        )
    return sheets


@login_required
@require_GET
def volunteer_report(request: HttpRequest) -> HttpResponse:
    if not permissions.can_volunteer_reports(request.user):
        raise PermissionDenied
    form = PeriodForm(request.GET or None)
    report = None
    if form.is_valid():
        report = services.volunteer_report(request.user, form.period)
        if _wants_xlsx(request):
            _log_export(
                request,
                "volunteers",
                date_from=str(form.period.date_from),
                date_to=str(form.period.date_to),
                with_names=report.with_names,
            )
            return xlsx_response(build_workbook(volunteer_sheets(report)), "otchet-po-volonteram")
    return render(request, "reports/volunteers.html", {"form": form, "report": report})


# ---------------------------------------------------------------------------
# ФТ-О-03
# ---------------------------------------------------------------------------
def funnel_sheets(report: services.FunnelReport) -> list[Sheet]:
    average = report.average_days_to_active if report.average_days_to_active is not None else "—"
    caption = [
        "Воронка волонтёров",
        f"Анкеты, поступившие за период: {report.period.label}",
    ]
    return [
        Sheet(
            "Воронка",
            ["Статус", "Дошли", "От предыдущего шага, %", "От анкет, %"],
            [(s.label, s.reached, s.from_previous, s.from_start) for s in report.steps],
            caption=[
                *caption,
                f"Выбыли, не став активными: {report.left_before_active}",
                f"Дней от анкеты до статуса «Активен» в среднем: {average}",
            ],
        ),
        Sheet(
            "Сейчас по статусам",
            ["Статус", "Волонтёров"],
            report.current,
            caption=["Все действующие карточки на дату выгрузки"],
        ),
    ]


@login_required
@require_GET
def funnel_report(request: HttpRequest) -> HttpResponse:
    if not permissions.can_volunteer_reports(request.user):
        raise PermissionDenied
    form = PeriodForm(request.GET or None)
    report = None
    if form.is_valid():
        report = services.funnel_report(request.user, form.period)
        if _wants_xlsx(request):
            _log_export(
                request,
                "funnel",
                date_from=str(form.period.date_from),
                date_to=str(form.period.date_to),
            )
            return xlsx_response(build_workbook(funnel_sheets(report)), "voronka-volonterov")
    return render(request, "reports/funnel.html", {"form": form, "report": report})


# ---------------------------------------------------------------------------
# ФТ-О-04: выгрузка списков
# ---------------------------------------------------------------------------
@login_required
@require_GET
def export_list(request: HttpRequest, kind: str) -> HttpResponse:
    """Список с теми же фильтрами и правами, что на экране, — в Excel."""
    if kind not in exports.EXPORTS:
        raise Http404
    export, sheet = exports.build(kind, request)
    return xlsx_response(build_workbook([sheet]), export.filename)
