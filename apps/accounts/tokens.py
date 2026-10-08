"""Сервисные токены внутреннего API (п. 7.5): бот и интеграции, у каждого — свои права.

Заголовок запроса: `Authorization: Bearer <токен>`. Сессия и CSRF здесь не участвуют.
"""

from __future__ import annotations

import datetime
import hashlib
import secrets
from typing import Any

from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from .models import ServiceToken

TOKEN_PREFIX = "crm_"  # noqa: S105 — префикс для узнаваемости, не секрет
KEYWORD = b"bearer"
# Отметка «последнее использование» обновляется не чаще раза в минуту — без записи
# в базу на каждый запрос бота
LAST_USED_PRECISION = datetime.timedelta(minutes=1)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_token(
    name: str, scopes: list[str], *, expires_at: datetime.datetime | None = None
) -> tuple[ServiceToken, str]:
    """Выпустить токен. Возвращает запись и сам токен — он больше нигде не хранится."""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token = ServiceToken.objects.create(
        name=name,
        scopes=sorted(set(scopes)),
        key_prefix=raw[:12],
        key_hash=hash_token(raw),
        expires_at=expires_at,
    )
    return token, raw


class ServiceTokenAuthentication(BaseAuthentication):
    """Запрос от имени сервиса: request.user — аноним, request.auth — ServiceToken."""

    def authenticate(self, request: Request) -> tuple[Any, ServiceToken] | None:
        header = get_authorization_header(request).split()
        if not header or header[0].lower() != KEYWORD:
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Неверный заголовок Authorization.")
        try:
            raw = header[1].decode()
        except UnicodeDecodeError as exc:
            raise exceptions.AuthenticationFailed("Неверный токен.") from exc
        token = ServiceToken.objects.usable().filter(key_hash=hash_token(raw)).first()
        if token is None:
            raise exceptions.AuthenticationFailed("Неверный или отозванный токен.")
        now = timezone.now()
        if token.last_used_at is None or now - token.last_used_at > LAST_USED_PRECISION:
            ServiceToken.objects.filter(pk=token.pk).update(last_used_at=now)
        return AnonymousUser(), token

    def authenticate_header(self, request: Request) -> str:
        return 'Bearer realm="api"'


class HasTokenScope(BasePermission):
    """Доступ только с токеном, у которого есть право `view.required_scope`."""

    message = "У токена нет прав на этот запрос."

    def has_permission(self, request: Request, view: Any) -> bool:
        token = request.auth
        scope = str(getattr(view, "required_scope", "") or "")
        return isinstance(token, ServiceToken) and bool(scope) and token.has_scope(scope)


class ServiceTokenScheme(OpenApiAuthenticationExtension):
    """Схема токена сервиса в документации OpenAPI (/api/v1/docs/)."""

    target_class = "apps.accounts.tokens.ServiceTokenAuthentication"
    name = "serviceToken"

    def get_security_definition(self, auto_schema: Any) -> dict[str, str]:
        return {
            "type": "http",
            "scheme": "bearer",
            "description": "Сервисный токен: manage.py issue_service_token (право «bot»).",
        }
