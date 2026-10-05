from collections.abc import Callable
from functools import update_wrapper
from typing import Any

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from unfold.sites import UnfoldAdminSite

from apps.accounts.otp import is_verified
from apps.accounts.roles import RoleCode, user_has_role


class CRMAdminSite(UnfoldAdminSite):
    """Админ-панель: справочники, пользователи, журнал аудита.

    Доступ — только роль «Администратор» после входа со вторым фактором.
    Собственной формы входа у админки нет: используется общий вход с 2FA.
    """

    def has_permission(self, request: HttpRequest) -> bool:
        return is_verified(request.user) and user_has_role(request.user, RoleCode.ADMIN)

    def admin_view(self, view: Callable[..., Any], cacheable: bool = False) -> Callable[..., Any]:
        inner = super().admin_view(view, cacheable)

        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
            # Сотрудник без роли администратора получает отказ сразу, без редиректа на вход
            if is_verified(request.user) and not self.has_permission(request):
                raise PermissionDenied
            return inner(request, *args, **kwargs)

        return update_wrapper(wrapper, inner)

    def password_change(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        # Единая смена пароля — с записью в журнал аудита
        return redirect("accounts:password_change")

    def login(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        if is_verified(request.user):
            raise PermissionDenied
        return redirect_to_login(request.get_full_path(), reverse("accounts:login"))
