"""Текущий пользователь запроса — для заполнения created_by / updated_by (п. 4.1 ТЗ)."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING

from django.http import HttpRequest, HttpResponse

if TYPE_CHECKING:
    from apps.accounts.models import User

_current_user: ContextVar[User | None] = ContextVar("current_user", default=None)


def get_current_user() -> User | None:
    return _current_user.get()


class CurrentUserMiddleware:
    """Сохраняет пользователя запроса в contextvar. Ставится после AuthenticationMiddleware."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = request.user if request.user.is_authenticated else None
        token = _current_user.set(user)
        try:
            return self.get_response(request)
        finally:
            _current_user.reset(token)


class acting_as:
    """Явно задать автора изменений вне запроса (Celery, команды, бот)."""

    def __init__(self, user: User | None) -> None:
        self.user = user

    def __enter__(self) -> None:
        self._token = _current_user.set(self.user)

    def __exit__(self, *exc: object) -> None:
        _current_user.reset(self._token)
