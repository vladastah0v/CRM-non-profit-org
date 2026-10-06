from typing import cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import User
from apps.accounts.roles import is_staff_member

from . import services
from .models import Notification


def _staff_user(request: HttpRequest) -> User:
    if not is_staff_member(request.user):
        raise PermissionDenied
    return cast(User, request.user)


@login_required
@require_GET
def notification_list(request: HttpRequest) -> HttpResponse:
    user = _staff_user(request)
    page = Paginator(Notification.objects.visible_to(user), 30).get_page(request.GET.get("page"))
    return render(request, "notifications/list.html", {"page": page})


@login_required
@require_POST
def notification_open(request: HttpRequest, pk: str) -> HttpResponse:
    """Отметить прочитанным и перейти к объекту. POST: GET не должен менять данные."""
    user = _staff_user(request)
    notification = get_object_or_404(Notification.objects.visible_to(user), pk=pk)
    services.mark_read(notification)
    # Ссылка формируется системой, но проверяется: только адрес внутри CRM
    if notification.url and url_has_allowed_host_and_scheme(
        notification.url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(notification.url)
    return redirect("notifications:list")


@login_required
@require_POST
def notification_read_all(request: HttpRequest) -> HttpResponse:
    count = services.mark_all_read(_staff_user(request))
    if count:
        messages.success(request, "Все уведомления отмечены прочитанными.")
    return redirect("notifications:list")
