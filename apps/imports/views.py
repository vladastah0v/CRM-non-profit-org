"""Экраны импорта (ФТ-А-05): загрузка, шаблоны, предпросмотр, запуск, отчёт об ошибках."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django.views.decorators.vary import vary_on_headers

from apps.accounts.roles import RoleCode, user_has_role
from apps.core.forms import StyledFormMixin
from apps.core.pages import apply_service_errors
from apps.core.xlsx import Sheet, build_workbook, xlsx_response

from . import importers, reader, services
from .models import ImportJob, ImportKind


class ImportForm(StyledFormMixin, forms.Form):
    kind = forms.ChoiceField(label="Что импортировать", choices=ImportKind.choices)
    file = forms.FileField(
        label="Файл",
        help_text=f"Excel (.xlsx) или CSV по шаблону, до 5 МБ и {reader.MAX_ROWS} строк.",
        widget=forms.ClearableFileInput(attrs={"accept": ".xlsx,.csv"}),
    )


def _require_admin(request: HttpRequest) -> None:
    if not user_has_role(request.user, RoleCode.ADMIN):
        raise PermissionDenied


def _job(request: HttpRequest, pk: str) -> ImportJob:
    _require_admin(request)
    return get_object_or_404(ImportJob.objects.visible_to(request.user), pk=pk)


@login_required
@require_http_methods(["GET", "POST"])
def import_list(request: HttpRequest) -> HttpResponse:
    _require_admin(request)
    form = ImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            job = services.start(
                form.cleaned_data["kind"], form.cleaned_data["file"], request=request
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Файл загружен — идёт проверка. Ничего ещё не сохранено.")
            return redirect(job)
    jobs = ImportJob.objects.visible_to(request.user).select_related("created_by")[:30]
    kinds = [(kind, label, importers.IMPORTERS[kind].columns) for kind, label in ImportKind.choices]
    return render(request, "imports/list.html", {"form": form, "jobs": jobs, "kinds": kinds})


@login_required
@require_GET
def template(request: HttpRequest, kind: str) -> HttpResponse:
    """Шаблон: лист с колонками (* — обязательные) и лист с пояснениями."""
    _require_admin(request)
    importer = importers.IMPORTERS.get(kind)
    if importer is None:
        raise Http404
    columns = importer.columns
    sheets = [
        Sheet("Данные", [f"{c.header}*" if c.required else c.header for c in columns], []),
        Sheet(
            "Пояснения",
            ["Колонка", "Обязательная", "Как заполнять"],
            [(c.header, c.required, c.help) for c in columns],
            caption=[
                f"Шаблон импорта: {importer.title}",
                "Заполняйте лист «Данные» с первой строки после заголовков. Даты — ДД.ММ.ГГГГ.",
                "Значения справочников — как в CRM; регистр не важен.",
            ],
        ),
    ]
    return xlsx_response(build_workbook(sheets), f"shablon-{kind}")


@login_required
@require_GET
@vary_on_headers("HX-Request")
def job_detail(request: HttpRequest, pk: str) -> HttpResponse:
    job = _job(request, pk)
    context = {"job": job, "summary": job.result or job.preview}
    htmx = getattr(request, "htmx", None)
    template_name = "imports/_job.html" if htmx else "imports/detail.html"
    return render(request, template_name, context)


@login_required
@require_POST
def job_confirm(request: HttpRequest, pk: str) -> HttpResponse:
    job = _job(request, pk)
    try:
        services.confirm(job, request=request)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Импорт запущен. О завершении придёт уведомление.")
    return redirect(job)


@login_required
@require_POST
def job_cancel(request: HttpRequest, pk: str) -> HttpResponse:
    job = _job(request, pk)
    try:
        services.cancel(job, request=request)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Импорт отменён, файл удалён.")
    return redirect(job)


@login_required
@require_GET
def job_errors(request: HttpRequest, pk: str) -> HttpResponse:
    """Отчёт об ошибках построчно — без значений из файла."""
    job = _job(request, pk)
    rows = [(e["row"], e["column"], e["message"]) for e in job.errors]
    rows += [(d["row"], "", d["message"]) for d in job.duplicates]
    sheet = Sheet(
        "Ошибки",
        ["Строка", "Колонка", "Что не так"],
        sorted(rows, key=lambda r: r[0]),
        caption=[f"{job.full_name}, загружен {job.created_at:%d.%m.%Y %H:%M}"],
    )
    return xlsx_response(build_workbook([sheet]), "oshibki-importa")
