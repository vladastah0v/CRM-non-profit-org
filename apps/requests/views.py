"""Экраны заявок (ФТ-З-01…02, ФТ-З-04) и оказанной помощи (ФТ-З-03)."""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django.views.decorators.vary import vary_on_headers

from apps.accounts.roles import RoleCode, user_has_role
from apps.beneficiaries import selectors as beneficiary_selectors
from apps.beneficiaries.models import Beneficiary
from apps.core.list_filters import RESET_PARAM, restore_or_save
from apps.core.pages import action_page, apply_service_errors

from . import permissions, services
from .filters import SAVED_PARAMS, RequestFilter
from .forms import AssistanceForm, HelpRequestForm, RequestStatusForm
from .models import Assistance, HelpRequest

LIST_KEY = "requests"
PAGE_SIZE = 25
# «Мои заявки» (ФТ-З-04): с чего начинается список у куратора без сохранённых фильтров
CURATOR_DEFAULT = "scope=mine&status=open"


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _managed_beneficiary(request: HttpRequest, pk: str) -> Beneficiary:
    """Подопечный, которому этот сотрудник создаёт заявки и регистрирует помощь."""
    beneficiary = beneficiary_selectors.get_managed_beneficiary(request.user, pk)
    if beneficiary.is_archived:
        raise PermissionDenied
    return beneficiary


def _visible_request(request: HttpRequest, pk: str) -> HelpRequest:
    if not permissions.can_view(request.user):
        raise PermissionDenied
    queryset = HelpRequest.objects.visible_to(request.user).select_related(
        "beneficiary", "assistance_type", "responsible"
    )
    return get_object_or_404(queryset, pk=pk)


def _managed_request(request: HttpRequest, pk: str) -> HelpRequest:
    help_request = _visible_request(request, pk)
    if not permissions.manages_request(request.user, help_request):
        raise PermissionDenied
    return help_request


# ---------------------------------------------------------------------------
# Заявки
# ---------------------------------------------------------------------------
@login_required
@require_GET
@vary_on_headers("HX-Request")
def request_list(request: HttpRequest) -> HttpResponse:
    if not permissions.can_view(request.user):
        raise PermissionDenied
    htmx = getattr(request, "htmx", None)
    is_htmx = htmx is not None and bool(htmx) and not htmx.history_restore_request
    if not is_htmx:
        saved = restore_or_save(request, LIST_KEY, SAVED_PARAMS)
        if saved is None and not request.GET and user_has_role(request.user, RoleCode.CURATOR):
            saved = CURATOR_DEFAULT
        if saved:
            return redirect(f"{reverse('requests:list')}?{saved}")
    elif request.GET:
        restore_or_save(request, LIST_KEY, SAVED_PARAMS)

    queryset = HelpRequest.objects.visible_to(request.user).select_related(
        "beneficiary", "assistance_type", "responsible"
    )
    request_filter = RequestFilter(request.GET, queryset=queryset, user=request.user)
    page = Paginator(request_filter.qs, PAGE_SIZE).get_page(request.GET.get("page"))
    context = {"filter": request_filter, "page": page, "reset_param": RESET_PARAM}
    template = "requests/_results.html" if is_htmx else "requests/list.html"
    return render(request, template, context)


@login_required
@require_http_methods(["GET", "POST"])
def request_create(request: HttpRequest, beneficiary_pk: str) -> HttpResponse:
    beneficiary = _managed_beneficiary(request, beneficiary_pk)
    form = HelpRequestForm(request.POST or None, initial={"responsible": beneficiary.curator_id})
    if request.method == "POST" and form.is_valid():
        try:
            help_request = services.create_request(
                beneficiary,
                form.service_data(),
                responsible=form.cleaned_data["responsible"],
                request=request,
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Заявка создана.")
            return redirect(help_request)
    return action_page(request, form, subject=beneficiary, title="Новая заявка", submit="Создать")


@login_required
@require_GET
def request_detail(request: HttpRequest, pk: str) -> HttpResponse:
    help_request = _visible_request(request, pk)
    manages = permissions.manages_request(request.user, help_request)
    assistance = help_request.assistance.filter(is_archived=False).select_related(
        "assistance_type", "program", "executor_user", "executor_volunteer"
    )
    context = {
        "r": help_request,
        "manages": manages,
        "can_open_card": Beneficiary.objects.visible_to(request.user)
        .filter(pk=help_request.beneficiary_id)
        .exists(),
        "assistance": assistance,
        "transitions": services.allowed_transitions(help_request) if manages else [],
        "history": help_request.status_changes.select_related("created_by"),
    }
    return render(request, "requests/detail.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def request_edit(request: HttpRequest, pk: str) -> HttpResponse:
    help_request = _managed_request(request, pk)
    if not help_request.is_open:
        messages.error(request, "Изменять можно только открытую заявку.")
        return redirect(help_request)
    form = HelpRequestForm(request.POST or None, initial=HelpRequestForm.initial_for(help_request))
    if request.method == "POST" and form.is_valid():
        try:
            services.update_request(
                help_request,
                form.service_data(),
                responsible=form.cleaned_data["responsible"],
                request=request,
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Заявка сохранена.")
            return redirect(help_request)
    return action_page(
        request, form, subject=help_request, title="Изменение заявки", submit="Сохранить"
    )


@login_required
@require_http_methods(["GET", "POST"])
def request_status(request: HttpRequest, pk: str) -> HttpResponse:
    """ФТ-З-02: смена статуса с проверкой перехода в сервисе."""
    help_request = _managed_request(request, pk)
    transitions = services.allowed_transitions(help_request)
    if not transitions or help_request.is_archived:
        messages.error(request, "Заявка закрыта — смена статуса недоступна.")
        return redirect(help_request)
    form = RequestStatusForm(request.POST or None, transitions=transitions)
    if request.method == "POST" and form.is_valid():
        try:
            services.change_status(help_request, **form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, f"Статус заявки: «{help_request.get_status_display()}».")
            return redirect(help_request)
    notes = []
    if "done" in transitions and not help_request.assistance.filter(is_archived=False).exists():
        notes.append("Чтобы отметить заявку выполненной, сначала зарегистрируйте оказанную помощь.")
    return action_page(
        request,
        form,
        subject=help_request,
        title="Смена статуса заявки",
        submit="Сменить статус",
        notes=notes,
        notes_title="Обратите внимание:",
    )


@login_required
@require_POST
def request_archive(request: HttpRequest, pk: str) -> HttpResponse:
    """Удаление — только администратору (п. 2.3): ошибочная заявка уходит в архив."""
    help_request = _visible_request(request, pk)
    if not user_has_role(request.user, RoleCode.ADMIN):
        raise PermissionDenied
    services.archive_request(help_request, request=request)
    messages.success(request, "Заявка переведена в архив.")
    return redirect(help_request.beneficiary)


# ---------------------------------------------------------------------------
# Оказанная помощь
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET", "POST"])
def assistance_add(request: HttpRequest, beneficiary_pk: str) -> HttpResponse:
    beneficiary = _managed_beneficiary(request, beneficiary_pk)
    initial: dict[str, object] = {"executor_user": request.user.pk}
    request_pk = _uuid_or_none(request.GET.get("request"))
    help_request = (
        HelpRequest.objects.open().filter(pk=request_pk, beneficiary=beneficiary).first()
        if request_pk
        else None
    )
    if help_request is not None:
        initial |= {"request": help_request.pk, "assistance_type": help_request.assistance_type_id}
    form = AssistanceForm(request.POST or None, beneficiary=beneficiary, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            services.register_assistance(
                beneficiary,
                form.service_data(),
                help_request=form.cleaned_data.get("request"),
                request=request,
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Оказанная помощь зарегистрирована.")
            linked = form.cleaned_data.get("request")
            return redirect(linked or beneficiary)
    return action_page(
        request, form, subject=beneficiary, title="Оказанная помощь", submit="Зарегистрировать"
    )


def _managed_assistance(request: HttpRequest, pk: str) -> Assistance:
    queryset = Assistance.objects.visible_to(request.user).select_related("beneficiary")
    assistance = get_object_or_404(queryset, pk=pk, is_archived=False)
    if not permissions.manages_beneficiary(request.user, assistance.beneficiary):
        raise PermissionDenied
    return assistance


@login_required
@require_http_methods(["GET", "POST"])
def assistance_edit(request: HttpRequest, pk: str) -> HttpResponse:
    assistance = _managed_assistance(request, pk)
    initial = {
        name: getattr(assistance, name)
        for name in (
            "assistance_type",
            "provided_on",
            "quantity",
            "unit",
            "amount",
            "program",
            "executor_user",
            "executor_volunteer",
            "comment",
        )
    }
    form = AssistanceForm(
        request.POST or None,
        beneficiary=assistance.beneficiary,
        with_request=False,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.update_assistance(assistance, form.service_data(), request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Запись о помощи сохранена.")
            return redirect(assistance.beneficiary)
    return action_page(
        request, form, subject=assistance.beneficiary, title="Оказанная помощь", submit="Сохранить"
    )


@login_required
@require_POST
def assistance_archive(request: HttpRequest, pk: str) -> HttpResponse:
    assistance = _managed_assistance(request, pk)
    try:
        services.archive_assistance(assistance, request=request)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Запись о помощи переведена в архив.")
    return redirect(assistance.beneficiary)
