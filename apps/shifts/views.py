"""Экраны смен (ФТ-С-01…04) и визитов к подопечным (ФТ-С-05)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django.views.decorators.vary import vary_on_headers

from apps.beneficiaries import selectors as beneficiary_selectors
from apps.beneficiaries.models import Beneficiary, CaseStatus
from apps.core.list_filters import RESET_PARAM, restore_or_save
from apps.core.pages import action_page, apply_service_errors
from apps.volunteers.models import Volunteer

from . import permissions, selectors, services
from .filters import SAVED_PARAMS, ShiftFilter, with_occupied
from .forms import (
    AssignmentStatusForm,
    AttendanceForm,
    HoursForm,
    PickFilterForm,
    ShiftCancelForm,
    ShiftForm,
    VisitForm,
)
from .models import OCCUPYING_STATUSES, Assignment, AssignmentStatus, Shift

LIST_KEY = "shifts"
PAGE_SIZE = 25
DEFAULT_QUERY = "period=upcoming"


@login_required
@require_GET
@vary_on_headers("HX-Request")
def shift_list(request: HttpRequest) -> HttpResponse:
    if not permissions.can_view(request.user):
        raise PermissionDenied
    htmx = getattr(request, "htmx", None)
    is_htmx = htmx is not None and bool(htmx) and not htmx.history_restore_request
    if not is_htmx:
        saved = restore_or_save(request, LIST_KEY, SAVED_PARAMS)
        if saved is None and not request.GET:
            saved = DEFAULT_QUERY
        if saved:
            return redirect(f"{reverse('shifts:list')}?{saved}")
    elif request.GET:
        restore_or_save(request, LIST_KEY, SAVED_PARAMS)

    queryset = with_occupied(
        Shift.objects.visible_to(request.user).select_related("shift_type", "district")
    )
    shift_filter = ShiftFilter(request.GET, queryset=queryset)
    page = Paginator(shift_filter.qs, PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "filter": shift_filter,
        "page": page,
        "reset_param": RESET_PARAM,
        "can_manage": permissions.can_manage(request.user),
    }
    template = "shifts/_results.html" if is_htmx else "shifts/list.html"
    return render(request, template, context)


def _shift_form_page(
    request: HttpRequest, form: ShiftForm, *, shift: Shift | None = None
) -> HttpResponse:
    return render(request, "shifts/form.html", {"form": form, "shift": shift})


@login_required
@require_http_methods(["GET", "POST"])
def shift_create(request: HttpRequest) -> HttpResponse:
    if not permissions.can_manage(request.user):
        raise PermissionDenied
    form = ShiftForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            shift = services.create_shift(
                form.service_data(), skills=form.cleaned_data["skills"], request=request
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Смена создана. Теперь подберите волонтёров.")
            return redirect(shift)
    return _shift_form_page(request, form)


@login_required
@require_http_methods(["GET", "POST"])
def shift_edit(request: HttpRequest, pk: str) -> HttpResponse:
    shift = selectors.get_managed_shift(request.user, pk)
    if shift.is_cancelled or shift.is_archived:
        messages.error(request, "Отменённую или архивную смену изменить нельзя.")
        return redirect(shift)
    # Форма получает отдельный экземпляр: ModelForm записывает в него введённые значения,
    # а сервису нужен исходный — чтобы понять, что изменилось
    form = ShiftForm(request.POST or None, instance=Shift.objects.get(pk=shift.pk))
    if request.method == "POST" and form.is_valid():
        try:
            services.update_shift(
                shift, form.service_data(), skills=form.cleaned_data["skills"], request=request
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Смена сохранена.")
            return redirect(shift)
    return _shift_form_page(request, form, shift=shift)


@login_required
@require_GET
def shift_detail(request: HttpRequest, pk: str) -> HttpResponse:
    shift = selectors.get_shift(request.user, pk)
    card = selectors.shift_card(request.user, shift)
    return render(request, "shifts/detail.html", {"s": shift, "card": card})


@login_required
@require_http_methods(["GET", "POST"])
def shift_cancel(request: HttpRequest, pk: str) -> HttpResponse:
    shift = selectors.get_managed_shift(request.user, pk)
    form = ShiftCancelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.cancel_shift(shift, reason=form.cleaned_data["reason"], request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Смена отменена. Записанным волонтёрам ушло сообщение.")
            return redirect(shift)
    return action_page(
        request,
        form,
        subject=shift,
        title="Отмена смены",
        submit="Отменить смену",
        intro="Все записанные волонтёры будут сняты со смены.",
    )


@login_required
@require_POST
def shift_archive(request: HttpRequest, pk: str) -> HttpResponse:
    """Ошибочная смена — в архив вместе с назначениями."""
    shift = selectors.get_managed_shift(request.user, pk)
    services.archive_shift(shift, request=request)
    messages.success(request, "Смена переведена в архив.")
    return redirect("shifts:list")


# ---------------------------------------------------------------------------
# Назначения на смену: подбор (ФТ-С-03), статусы, явка (ФТ-С-04)
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET", "POST"])
def shift_pick(request: HttpRequest, pk: str) -> HttpResponse:
    shift = selectors.get_managed_shift(request.user, pk)
    if shift.is_cancelled or shift.is_archived:
        messages.error(request, "Смена отменена — назначать на неё нельзя.")
        return redirect(shift)
    if request.method == "POST":
        ids = request.POST.getlist("volunteer")
        volunteers = Volunteer.objects.visible_to(request.user).filter(pk__in=ids[:50])
        assigned, errors = 0, []
        for volunteer in volunteers:
            try:
                services.assign_to_shift(shift, volunteer, request=request)
            except ValidationError as exc:
                errors.append(f"{volunteer.full_name}: {' '.join(exc.messages)}")
            else:
                assigned += 1
        if assigned:
            messages.success(request, f"Назначено волонтёров: {assigned}.")
        for error in errors:
            messages.error(request, error)
        if not assigned and not errors:
            messages.error(request, "Отметьте хотя бы одного волонтёра.")
            return redirect(request.get_full_path())
        return redirect(shift)

    params = request.GET if request.GET else {"skills": "on"}
    filter_form = PickFilterForm(params)
    filter_form.is_valid()
    options = filter_form.cleaned_data
    candidates = selectors.suggest_volunteers(
        shift,
        query=options.get("q", ""),
        only_skills=options.get("skills", False),
        only_district=options.get("district", False),
        only_available=options.get("available", False),
    )
    context = {
        "s": shift,
        "filter_form": filter_form,
        "candidates": candidates,
        "occupied": services.occupied_places(shift),
        "required_skills": list(shift.skills.all()),
    }
    return render(request, "shifts/pick.html", context)


def _managed_assignment(request: HttpRequest, pk: str) -> Assignment:
    if not permissions.can_manage(request.user):
        raise PermissionDenied
    queryset = Assignment.objects.visible_to(request.user).select_related(
        "shift", "volunteer", "beneficiary"
    )
    return get_object_or_404(queryset, pk=pk, is_archived=False)


@login_required
@require_http_methods(["GET", "POST"])
def assignment_status(request: HttpRequest, pk: str) -> HttpResponse:
    """Статус одного назначения: подтвердил, отказался, снят, пришёл (с часами), не пришёл."""
    assignment = _managed_assignment(request, pk)
    transitions = services.allowed_transitions(assignment)
    if not transitions:
        messages.error(request, "Сейчас статус этого назначения изменить нельзя.")
        return redirect(assignment)
    form = AssignmentStatusForm(request.POST or None, transitions=transitions)
    if request.method == "POST" and form.is_valid():
        try:
            services.change_assignment_status(
                assignment,
                form.cleaned_data["to_status"],
                hours=form.cleaned_data["hours"],
                comment=form.cleaned_data["comment"],
                request=request,
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, f"Статус: «{assignment.get_status_display()}».")
            return redirect(assignment)
    return action_page(
        request, form, subject=assignment, title="Статус назначения", submit="Сохранить"
    )


@login_required
@require_http_methods(["GET", "POST"])
def assignment_hours(request: HttpRequest, pk: str) -> HttpResponse:
    assignment = _managed_assignment(request, pk)
    if assignment.status != AssignmentStatus.ATTENDED:
        messages.error(request, "Часы вносятся только для статуса «Пришёл».")
        return redirect(assignment)
    form = HoursForm(request.POST or None, initial={"hours": assignment.hours})
    if request.method == "POST" and form.is_valid():
        try:
            services.update_hours(assignment, form.cleaned_data["hours"], request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Часы сохранены.")
            return redirect(assignment)
    return action_page(request, form, subject=assignment, title="Часы", submit="Сохранить")


@login_required
@require_http_methods(["GET", "POST"])
def shift_attendance(request: HttpRequest, pk: str) -> HttpResponse:
    """Лист явки после начала смены: пришёл с часами или не пришёл (ФТ-С-04)."""
    shift = selectors.get_managed_shift(request.user, pk)
    card = selectors.shift_card(request.user, shift)
    if not card.can_mark_attendance:
        messages.error(request, "Явку отмечают после начала смены, если на неё кто-то записан.")
        return redirect(shift)
    assignments = [
        row.assignment
        for row in card.rows
        if row.assignment.status in (*OCCUPYING_STATUSES, AssignmentStatus.NO_SHOW)
    ]
    form = AttendanceForm(request.POST or None, assignments=assignments)
    if request.method == "POST" and form.is_valid():
        saved, errors = 0, []
        for assignment, attended, hours in form.marks():
            try:
                services.record_attendance(
                    assignment, attended=attended, hours=hours, request=request
                )
            except ValidationError as exc:
                errors.append(f"{assignment.volunteer.full_name}: {' '.join(exc.messages)}")
            else:
                saved += 1
        for error in errors:
            messages.error(request, error)
        if saved:
            messages.success(request, f"Отметки сохранены: {saved}.")
        return redirect(shift)
    return render(request, "shifts/attendance.html", {"s": shift, "form": form})


# ---------------------------------------------------------------------------
# Визиты к подопечным (ФТ-С-05)
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET", "POST"])
def visit_create(request: HttpRequest, beneficiary_pk: str) -> HttpResponse:
    if not permissions.can_manage(request.user):
        raise PermissionDenied
    beneficiary = beneficiary_selectors.get_beneficiary(request.user, beneficiary_pk)
    if beneficiary.is_archived or beneficiary.status != CaseStatus.IN_WORK:
        messages.error(request, "Визиты назначаются только по случаю в статусе «В работе».")
        return redirect(beneficiary)
    form = VisitForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            visits = services.create_visits(
                beneficiary,
                data["volunteer"],
                starts_at=data["starts_at"],
                ends_at=data["ends_at"],
                task_comment=data["task_comment"],
                program=data["program"],
                repeat_weeks=data["repeat_weeks"],
                request=request,
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            count = len(visits)
            messages.success(
                request, "Визит назначен." if count == 1 else f"Назначено визитов: {count}."
            )
            return redirect(beneficiary)
    return action_page(
        request,
        form,
        subject=beneficiary,
        title="Визит волонтёра",
        submit="Назначить",
        intro="Волонтёр увидит в боте только имя подопечного, адрес, телефон и комментарий "
        "к заданию — и только пока визит не прошёл.",
    )


@login_required
@require_GET
def visit_detail(request: HttpRequest, pk: str) -> HttpResponse:
    visit = selectors.get_visit(request.user, pk)
    can_manage = permissions.can_manage(request.user)
    context = {
        "visit": visit,
        "can_manage": can_manage,
        "transitions": services.allowed_transitions(visit) if can_manage else [],
        "can_open_card": visit.beneficiary_id is not None
        and Beneficiary.objects.visible_to(request.user).filter(pk=visit.beneficiary_id).exists(),
        "history": visit.status_changes.select_related("created_by"),
    }
    return render(request, "shifts/visit_detail.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def visit_edit(request: HttpRequest, pk: str) -> HttpResponse:
    visit = _managed_assignment(request, pk)
    if (
        not visit.is_visit
        or not visit.is_pending
        or visit.has_started
        or visit.visit_starts_at is None
        or visit.visit_ends_at is None
    ):
        messages.error(request, "Изменить можно только предстоящий визит.")
        return redirect(visit)
    starts, ends = (
        timezone.localtime(visit.visit_starts_at),
        timezone.localtime(visit.visit_ends_at),
    )
    initial = {
        "date": starts.date(),
        "time_from": starts.time(),
        "time_to": ends.time(),
        "task_comment": visit.task_comment,
        "program": visit.program_id,
    }
    form = VisitForm(request.POST or None, initial=initial, edit=True)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            services.update_visit(
                visit,
                starts_at=data["starts_at"],
                ends_at=data["ends_at"],
                task_comment=data["task_comment"],
                program=data["program"],
                request=request,
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Визит сохранён.")
            return redirect(visit)
    return action_page(request, form, subject=visit, title="Изменение визита", submit="Сохранить")
