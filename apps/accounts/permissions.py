from typing import Any

from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from .otp import is_verified
from .roles import is_staff_member


class IsVerifiedStaff(BasePermission):
    """API для сотрудников: вход выполнен, второй фактор подтверждён, есть роль сотрудника."""

    message = "Нужен вход с двухфакторной аутентификацией."

    def has_permission(self, request: Request, view: Any) -> bool:
        return is_verified(request.user) and is_staff_member(request.user)
