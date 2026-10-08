"""Экраны взаимодействий (ФТ-ВЗ-01) и задач (ФТ-ВЗ-02)."""

from __future__ import annotations

from typing import cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.models import User
from apps.accounts.roles import RoleCode, user_has_role
from apps.core.pages import action_page, apply_service_errors

from . import selectors, services
from .forms import InteractionForm, TaskForm
from .models import Interaction, Task, TaskStatus


def _staff(request: HttpRequest) -> None:
    """Взаимодействия и задачи — тем, кто работает с карточками (матрица 2.2: у аналитика
    доступа нет)."""
    if not user_has_role(request.user, RoleCode.ADMIN, RoleCode.COORDINATOR, RoleCode.CURATOR):
        raise PermissionDenied


# ---------------------------------------------------------------------------
# Взаимодействия
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET", "POST"])
def interaction_add(request: HttpRequest, kind: str, pk: str) -> HttpResponse:
    _staff(request)
    card = selectors.get_card(request.user, kind, pk, for_write=True)
    form = InteractionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.add_interaction(card, form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Запись добавлена.")
            return redirect(card)
    return action_page(request, form, subject=card, title="Взаимодействие", submit="Сохранить")


def _editable_interaction(request: HttpRequest, pk: str) -> Interaction:
    _staff(request)
    interaction = get_object_or_404(
        Interaction.objects.visible_to(request.user).filter(is_archived=False), pk=pk
    )
    if not services.can_edit_interaction(request.user, interaction):
        raise PermissionDenied
    return interaction


@login_required
@require_http_methods(["GET", "POST"])
def interaction_edit(request: HttpRequest, pk: str) -> HttpResponse:
    interaction = _editable_interaction(request, pk)
    initial = {
        "kind": interaction.kind,
        "happened_at": interaction.happened_at,
        "text": interaction.text,
    }
    form = InteractionForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_interaction(interaction, form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Запись сохранена.")
            return redirect(interaction.card)
    return action_page(
        request, form, subject=interaction.card, title="Взаимодействие", submit="Сохранить"
    )


@login_required
@require_POST
def interaction_archive(request: HttpRequest, pk: str) -> HttpResponse:
    interaction = _editable_interaction(request, pk)
    services.archive_interaction(interaction, request=request)
    messages.success(request, "Запись убрана.")
    return redirect(interaction.card)


# ---------------------------------------------------------------------------
# Задачи
# ---------------------------------------------------------------------------
TASK_VIEWS = {
    "mine": "Мне поручено",
    "created": "Поставлено мной",
    "closed": "Закрытые",
}


@login_required
@require_GET
def task_list(request: HttpRequest) -> HttpResponse:
    """«Мои задачи»: открытые, по сроку; просроченные подсвечены."""
    _staff(request)
    user = cast(User, request.user)
    show = request.GET.get("show", "mine")
    if show not in TASK_VIEWS:
        show = "mine"
    tasks = (
        Task.objects.visible_to(user)
        .filter(is_archived=False)
        .select_related("assignee", "created_by", "volunteer", "beneficiary")
    )
    if show == "mine":
        tasks = tasks.filter(assignee=user, status=TaskStatus.OPEN).order_by("due_date")
    elif show == "created":
        tasks = tasks.filter(created_by=user, status=TaskStatus.OPEN).order_by("due_date")
    else:
        tasks = tasks.filter(assignee=user).exclude(status=TaskStatus.OPEN)
        tasks = tasks.order_by("-done_at")
    page = Paginator(tasks, 30).get_page(request.GET.get("page"))
    return render(
        request, "interactions/task_list.html", {"page": page, "show": show, "views": TASK_VIEWS}
    )


@login_required
@require_http_methods(["GET", "POST"])
def task_add(request: HttpRequest, kind: str, pk: str) -> HttpResponse:
    _staff(request)
    card = selectors.get_card(request.user, kind, pk, for_write=True)
    form = TaskForm(
        request.POST or None,
        assignees=services.assignees_for(card),
        initial={"assignee": request.user.pk},
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.add_task(card, form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Задача поставлена.")
            return redirect(card)
    return action_page(request, form, subject=card, title="Задача", submit="Поставить")


def _manageable_task(request: HttpRequest, pk: str) -> Task:
    _staff(request)
    task = get_object_or_404(Task.objects.visible_to(request.user).filter(is_archived=False), pk=pk)
    if not services.can_manage_task(request.user, task):
        raise PermissionDenied
    return task


@login_required
@require_http_methods(["GET", "POST"])
def task_edit(request: HttpRequest, pk: str) -> HttpResponse:
    task = _manageable_task(request, pk)
    if task.status != TaskStatus.OPEN:
        messages.error(request, "Задача закрыта.")
        return redirect(task.card)
    form = TaskForm(
        request.POST or None,
        assignees=services.assignees_for(task.card),
        initial={"text": task.text, "due_date": task.due_date, "assignee": task.assignee_id},
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.update_task(task, form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Задача сохранена.")
            return redirect(task.card)
    return action_page(request, form, subject=task.card, title="Задача", submit="Сохранить")


@login_required
@require_POST
def task_close(request: HttpRequest, pk: str) -> HttpResponse:
    task = _manageable_task(request, pk)
    done = request.POST.get("result") != "cancel"
    try:
        services.close_task(task, done=done, request=request)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Задача выполнена." if done else "Задача отменена.")
    next_url = request.POST.get("next")
    if next_url == "list":
        return redirect("interactions:tasks")
    return redirect(task.card)
