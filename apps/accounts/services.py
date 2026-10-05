"""Вход в систему: пароль + TOTP, блокировка после неудачных попыток (НФТ-ИБ-02, НФТ-ИБ-03)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.hashers import make_password
from django.http import HttpRequest
from django.utils import timezone

from apps.audit.models import AuditAction
from apps.audit.services import log_event

from . import lockout
from .models import SecureTOTPDevice, User
from .roles import is_staff_member


class LoginError(StrEnum):
    INVALID = "invalid"
    LOCKED = "locked"
    NOT_STAFF = "not_staff"


LOGIN_ERROR_MESSAGES = {
    LoginError.INVALID: "Неверный логин или пароль.",
    LoginError.LOCKED: (
        "Слишком много неудачных попыток входа. Вход временно заблокирован, "
        "повторите через {minutes} минут."
    ),
    LoginError.NOT_STAFF: (
        "Доступ к веб-интерфейсу есть только у сотрудников фонда. "
        "Волонтёры работают через Telegram-бота."
    ),
}


def login_error_message(error: LoginError) -> str:
    return LOGIN_ERROR_MESSAGES[error].format(minutes=settings.LOGIN_LOCKOUT_MINUTES)


@dataclass(frozen=True)
class LoginResult:
    user: User | None
    error: LoginError | None = None


def _burn_hash(password: str) -> None:
    """Хэширование «вхолостую»: ответ по несуществующему или заблокированному логину
    занимает столько же времени, сколько по существующему (защита от перебора логинов)."""
    User().set_password(password)


def authenticate_password(request: HttpRequest, username: str, password: str) -> LoginResult:
    username = username.strip()
    user = User.objects.filter(username__iexact=username).first()

    if lockout.is_locked(username, user):
        _burn_hash(password)
        _audit_failure(request, username, user, locked=True)
        return LoginResult(None, LoginError.LOCKED)

    authenticated = None
    if user is None:
        _burn_hash(password)
    else:
        authenticated = authenticate(request, username=user.username, password=password)

    if authenticated is None:
        _audit_failure(request, username, user)
        locked = record_failure(request, username, user)
        return LoginResult(None, LoginError.LOCKED if locked else LoginError.INVALID)

    # Пока шла проверка пароля, параллельный запрос мог заблокировать учётную запись
    if lockout.is_locked(username, authenticated):
        return LoginResult(None, LoginError.LOCKED)

    if not is_staff_member(authenticated):
        log_event(AuditAction.LOGIN_FAILED, request=request, user=user, extra={"not_staff": True})
        return LoginResult(None, LoginError.NOT_STAFF)

    return LoginResult(authenticated)


def _audit_failure(
    request: HttpRequest, username: str, user: User | None, *, locked: bool = False
) -> None:
    extra: dict[str, object] = {"locked": True} if locked else {}
    if user is None:
        # Введённый «логин» в журнал не пишется: это может оказаться пароль или ФИО
        extra["login_sha256"] = lockout.login_hash(username)[:16]
    log_event(AuditAction.LOGIN_FAILED, request=request, user=user, extra=extra)


def record_failure(request: HttpRequest, username: str, user: User | None) -> bool:
    """Неверный пароль или код. Возвращает True, если вход только что заблокирован."""
    just_locked = lockout.register_failure(username, user)
    if just_locked and user is not None:
        log_event(AuditAction.ACCOUNT_LOCKED, request=request, user=user)
    return just_locked


# ---------------------------------------------------------------------------
# Двухфакторная аутентификация
# ---------------------------------------------------------------------------
ENROLLMENT_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих O/0, I/1


def get_confirmed_device(user: User) -> SecureTOTPDevice | None:
    return SecureTOTPDevice.objects.filter(user=user, confirmed=True).first()


def get_or_create_setup_device(user: User) -> SecureTOTPDevice:
    """Неподтверждённое устройство — создаётся только после проверки кода подключения."""
    device = SecureTOTPDevice.objects.filter(user=user, confirmed=False).first()
    if device is None:
        device = SecureTOTPDevice.objects.create(
            user=user, name="Приложение-аутентификатор", confirmed=False
        )
    return device


def issue_enrollment_code(request: HttpRequest | None, user: User) -> str:
    """Новый сотрудник или потерянный телефон: старые устройства удаляются, выдаётся
    одноразовый код подключения. Администратор передаёт его лично или по телефону.

    Удаление устройства сразу снимает подтверждение второго фактора во всех открытых
    сессиях пользователя: OTPMiddleware больше не находит устройство.
    """
    SecureTOTPDevice.objects.filter(user=user).delete()
    raw = "".join(secrets.choice(ENROLLMENT_ALPHABET) for _ in range(8))
    user.otp_enrollment_code = make_password(raw)
    user.otp_enrollment_expires = timezone.now() + timedelta(
        hours=settings.OTP_ENROLLMENT_TTL_HOURS
    )
    user.save(update_fields=["otp_enrollment_code", "otp_enrollment_expires"])
    log_event(AuditAction.TWO_FACTOR_RESET, request=request, obj=user)
    return f"{raw[:4]}-{raw[4:]}"


def consume_enrollment_code(user: User) -> None:
    user.otp_enrollment_code = ""
    user.otp_enrollment_expires = None
    user.save(update_fields=["otp_enrollment_code", "otp_enrollment_expires"])
