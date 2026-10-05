import logging

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


@login_required
def home(request: HttpRequest) -> HttpResponse:
    return render(request, "core/home.html")


@never_cache
@require_GET
def healthz(request: HttpRequest) -> JsonResponse:
    """Проверка доступности для мониторинга (Uptime Kuma). Не раскрывает детали ошибок."""
    checks: dict[str, bool] = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["db"] = True
    except Exception:
        logger.exception("Healthcheck: база данных недоступна")
        checks["db"] = False
    try:
        cache.set("healthz", "1", timeout=5)
        checks["cache"] = cache.get("healthz") == "1"
    except Exception:
        logger.exception("Healthcheck: кэш недоступен")
        checks["cache"] = False
    ok = all(checks.values())
    return JsonResponse({"status": "ok" if ok else "fail", **checks}, status=200 if ok else 503)


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    return render(request, "errors/403_csrf.html", status=403)
