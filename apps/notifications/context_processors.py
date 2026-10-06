from typing import Any

from django.http import HttpRequest

from apps.accounts.otp import is_verified
from apps.accounts.roles import is_staff_member

from .services import unread_count


def notifications(request: HttpRequest) -> dict[str, Any]:
    """Счётчик для колокольчика в шапке.

    Считается при загрузке страницы, без периодического опроса сервера: фоновые запросы
    продлевали бы сессию и отменили бы выход после 30 минут бездействия (НФТ-ИБ-04).
    """
    user = getattr(request, "user", None)
    if user is None or not is_verified(user) or not is_staff_member(user):
        return {}
    return {"unread_notifications": unread_count(user)}
