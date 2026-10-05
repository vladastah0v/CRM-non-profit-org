from typing import Any

from django.contrib.auth.signals import user_logged_out
from django.dispatch import receiver
from django.http import HttpRequest

from apps.accounts.otp import is_verified

from .models import AuditAction
from .services import log_event


@receiver(user_logged_out)
def audit_logout(sender: Any, request: HttpRequest, user: Any, **kwargs: Any) -> None:
    # Прерванный вход (пароль без кода) — не «выход»: события входа в журнале и не было
    if user is not None and is_verified(user):
        log_event(AuditAction.LOGOUT, request=request, user=user)
