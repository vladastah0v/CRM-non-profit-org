"""Вход: шаг 1 — логин и пароль, шаг 2 — код TOTP.

Первое подключение 2FA: сначала одноразовый код от администратора, только потом QR-код.
Без кода подключения знание одного пароля не даёт привязать своё устройство.
"""

from __future__ import annotations

import base64
import time
from io import BytesIO
from typing import Any, cast

import qrcode
import qrcode.image.svg
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST
from django.views.generic import View
from django_otp import login as otp_login

from apps.audit.models import AuditAction
from apps.audit.services import log_event

from . import lockout, services
from .forms import EnrollmentCodeForm, LoginForm, StyledPasswordChangeForm, TokenForm
from .models import SecureTOTPDevice, User
from .otp import is_verified

NEXT_SESSION_KEY = "login_next"
FIRST_FACTOR_AT = "login_first_factor_at"
ENROLL_OK_FOR = "otp_enrollment_ok_for"
WRONG_CODE_MESSAGE = "Неверный код. Проверьте время на телефоне и попробуйте ещё раз."
WRONG_ENROLLMENT_MESSAGE = "Неверный или просроченный код подключения."


def _user(request: HttpRequest) -> User:
    """Пользователь запроса во view под login_required."""
    return cast(User, request.user)


def _safe_next(request: HttpRequest, value: str | None) -> str:
    if value and url_has_allowed_host_and_scheme(
        value, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return value
    return reverse("core:home")


def _abort_login(request: HttpRequest, message: str) -> HttpResponse:
    # Вход не завершён — выход без записи «Выход» в журнал (см. audit.signals)
    auth_logout(request)
    messages.error(request, message)
    return redirect("accounts:login")


def _check_pending_login(request: HttpRequest, *, enrollment: bool = False) -> HttpResponse | None:
    """Шаг ввода кода: учётная запись не заблокирована и время на второй фактор не истекло
    (5 минут; при первом подключении телефона — 15: нужно установить приложение)."""
    user = _user(request)
    if lockout.is_locked(user.username, user):
        return _abort_login(request, services.login_error_message(services.LoginError.LOCKED))
    limit = (
        settings.OTP_ENROLLMENT_LOGIN_SECONDS if enrollment else settings.OTP_PENDING_LOGIN_SECONDS
    )
    started = request.session.get(FIRST_FACTOR_AT)
    if started is None or time.time() - started > limit:
        return _abort_login(request, "Время на ввод кода истекло. Войдите ещё раз.")
    return None


def _finish_login(request: HttpRequest, device: SecureTOTPDevice) -> HttpResponse:
    user = _user(request)
    # Блокировка могла наступить между открытием страницы и вводом верного кода
    if lockout.is_locked(user.username, user):
        return _abort_login(request, services.login_error_message(services.LoginError.LOCKED))
    otp_login(request, device)
    request.session.cycle_key()  # повышение привилегий — новый идентификатор сессии
    request.session.pop(FIRST_FACTOR_AT, None)
    request.session.pop(ENROLL_OK_FOR, None)
    lockout.reset(user.username, user)
    log_event(AuditAction.LOGIN, request=request)
    return HttpResponseRedirect(_safe_next(request, request.session.pop(NEXT_SESSION_KEY, None)))


def _fail(
    request: HttpRequest, form: forms.Form, field: str, message: str, action: AuditAction
) -> HttpResponse | None:
    """Неверный код. Если вход заблокирован — выход из системы."""
    user = _user(request)
    log_event(action, request=request, user=user)
    if services.record_failure(request, user.username, user):
        return _abort_login(request, services.login_error_message(services.LoginError.LOCKED))
    form.add_error(field, message)
    return None


@method_decorator(
    [sensitive_post_parameters("password"), csrf_protect, never_cache], name="dispatch"
)
class LoginView(View):
    template_name = "accounts/login.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if is_verified(request.user):
            return redirect("core:home")
        return render(request, self.template_name, {"form": LoginForm(), "next": self._next()})

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = LoginForm(request.POST)
        if form.is_valid():
            result = services.authenticate_password(
                request, form.cleaned_data["username"], form.cleaned_data["password"]
            )
            if result.user is not None:
                next_url = self._next()
                auth_login(request, result.user)  # новая сессия, но без второго фактора
                request.session[NEXT_SESSION_KEY] = next_url
                request.session[FIRST_FACTOR_AT] = time.time()
                return redirect("accounts:otp_verify")
            error = result.error or services.LoginError.INVALID
            form.add_error(None, services.login_error_message(error))
        return render(request, self.template_name, {"form": form, "next": self._next()})

    def _next(self) -> str:
        return _safe_next(
            self.request, self.request.POST.get("next") or self.request.GET.get("next")
        )


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def otp_verify(request: HttpRequest) -> HttpResponse:
    if is_verified(request.user):
        return redirect("core:home")
    device = services.get_confirmed_device(_user(request))
    if device is None:
        return redirect("accounts:otp_setup")  # там свои проверки и лимит времени
    if (response := _check_pending_login(request)) is not None:
        return response

    form = TokenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if device.verify_token(form.cleaned_data["token"]):
            return _finish_login(request, device)
        response = _fail(request, form, "token", WRONG_CODE_MESSAGE, AuditAction.OTP_FAILED)
        if response is not None:
            return response
    return render(request, "accounts/otp_verify.html", {"form": form})


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
@sensitive_post_parameters("enrollment_code")
def otp_setup(request: HttpRequest) -> HttpResponse:
    """Подключение 2FA: код подключения от администратора → QR-код → код из приложения."""
    if is_verified(request.user):
        return redirect("core:home")
    if (response := _check_pending_login(request, enrollment=True)) is not None:
        return response
    user = _user(request)
    if services.get_confirmed_device(user) is not None:
        return redirect("accounts:otp_verify")

    if request.session.get(ENROLL_OK_FOR) != str(user.pk):
        return _enrollment_code_step(request, user)

    device = services.get_or_create_setup_device(user)
    form = TokenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if device.verify_token(form.cleaned_data["token"]):
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            services.consume_enrollment_code(user)
            log_event(AuditAction.TWO_FACTOR_ENABLED, request=request, obj=user)
            return _finish_login(request, device)
        response = _fail(request, form, "token", WRONG_CODE_MESSAGE, AuditAction.OTP_FAILED)
        if response is not None:
            return response

    secret = base64.b32encode(device.bin_key).decode()
    context = {
        "form": form,
        "qr_data_uri": _qr_data_uri(device.config_url),
        "secret": " ".join(secret[i : i + 4] for i in range(0, len(secret), 4)),
    }
    return render(request, "accounts/otp_setup.html", context)


def _enrollment_code_step(request: HttpRequest, user: User) -> HttpResponse:
    if not user.has_enrollment_code():
        return render(request, "accounts/otp_no_code.html", status=403)
    form = EnrollmentCodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if user.check_enrollment_code(form.cleaned_data["enrollment_code"]):
            request.session[ENROLL_OK_FOR] = str(user.pk)
            return redirect("accounts:otp_setup")
        response = _fail(
            request, form, "enrollment_code", WRONG_ENROLLMENT_MESSAGE, AuditAction.OTP_FAILED
        )
        if response is not None:
            return response
    return render(request, "accounts/otp_enrollment_code.html", {"form": form})


def _qr_data_uri(text: str) -> str:
    """QR-код как data: URI — без встроенных скриптов и стилей (совместимо с CSP)."""
    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10)
    buffer = BytesIO()
    image.save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode()


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    auth_logout(request)  # сигнал user_logged_out пишет событие в журнал аудита
    messages.info(request, "Вы вышли из системы.")
    return redirect("accounts:login")


class PasswordChangeView(auth_views.PasswordChangeView):
    template_name = "accounts/password_change.html"
    form_class = StyledPasswordChangeForm
    success_url = reverse_lazy("core:home")

    def form_valid(self, form: Any) -> HttpResponse:
        response = super().form_valid(form)
        update_session_auth_hash(self.request, form.user)
        log_event(AuditAction.PASSWORD_CHANGED, request=self.request, obj=form.user)
        messages.success(self.request, "Пароль изменён.")
        return response
