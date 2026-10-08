"""Без второго фактора дальше страницы ввода кода не пройти (НФТ-ИБ-02).

Страницы сотрудников не остаются в кэше браузера (замечание проверки ZAP, этап 9).
"""

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.utils.cache import add_never_cache_headers

from .otp import is_verified

# Пути, доступные пользователю, который ввёл пароль, но ещё не ввёл код.
# /api/ сюда не входит: API для сессий тоже требует второй фактор.
ALLOWED_PREFIXES = ("/static/", "/healthz/")


class RequireOTPMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = request.user
        if user.is_authenticated and not is_verified(user) and not self._is_allowed(request):
            target = reverse("accounts:otp_verify")
            if request.path_info.startswith("/api/"):
                return JsonResponse(
                    {"detail": "Нужен вход с двухфакторной аутентификацией."}, status=403
                )
            if getattr(request, "htmx", False):
                response = HttpResponse(status=204)
                response["HX-Redirect"] = target
                return response
            return HttpResponseRedirect(target)
        return self.get_response(request)

    @staticmethod
    def _is_allowed(request: HttpRequest) -> bool:
        path = request.path_info
        if path.startswith(ALLOWED_PREFIXES):
            return True
        allowed = {
            reverse("accounts:login"),
            reverse("accounts:logout"),
            reverse("accounts:otp_setup"),
            reverse("accounts:otp_verify"),
        }
        return path in allowed or path == settings.STATIC_URL


class NoStoreForSignedInMiddleware:
    """Ответы вошедшему сотруднику — с запретом кэширования: на общем компьютере страница
    с ФИО не должна остаться на диске браузера. Заголовок, заданный view (файлы, выгрузки),
    не меняется. Ставится после AuthenticationMiddleware."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if request.user.is_authenticated and not response.has_header("Cache-Control"):
            add_never_cache_headers(response)
        return response
