"""Вход: пароль + 2FA (НФТ-ИБ-02), блокировка (НФТ-ИБ-03), тайм-аут (НФТ-ИБ-04)."""

import time
from datetime import timedelta
from unittest import mock

import pytest
from django.conf import settings
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.accounts import lockout
from apps.accounts.models import SecureTOTPDevice, User
from apps.accounts.services import issue_enrollment_code
from apps.audit.models import AuditAction, AuditLog
from conftest import PASSWORD, current_token, otp_device

pytestmark = pytest.mark.django_db

LOGIN = reverse("accounts:login")
VERIFY = reverse("accounts:otp_verify")
SETUP = reverse("accounts:otp_setup")
HOME = reverse("core:home")
WRONG = "неверный-пароль"


def login(client, username, password=PASSWORD, **extra):
    return client.post(LOGIN, {"username": username, "password": password, **extra})


def body(response) -> str:
    return response.content.decode()


# --- Второй фактор (НФТ-ИБ-02) -------------------------------------------------------------


def test_nft_ib_02_password_alone_is_not_enough(client, make_user):
    user = make_user("coordinator")
    otp_device(user)
    response = login(client, user.username)
    assert response.status_code == 302 and response["Location"] == VERIFY
    response = client.get(HOME)
    assert response.status_code == 302 and response["Location"] == VERIFY


def test_nft_ib_02_full_login_with_totp(client, make_user):
    user = make_user("coordinator")
    device = otp_device(user)
    login(client, user.username)
    password_session = client.session.session_key
    response = client.post(VERIFY, {"token": current_token(device)})
    assert response.status_code == 302 and response["Location"] == HOME
    assert client.get(HOME).status_code == 200
    # После второго фактора — новый идентификатор сессии (повышение привилегий)
    assert client.session.session_key != password_session
    assert AuditLog.objects.filter(user=user, action=AuditAction.LOGIN).exists()


def test_first_login_without_enrollment_code_cannot_bind_device(client, make_user):
    """Знание одного пароля не позволяет привязать своё устройство (защита от захвата)."""
    user = make_user("curator")
    login(client, user.username)
    assert client.get(VERIFY)["Location"] == SETUP
    response = client.get(SETUP)
    assert response.status_code == 403
    assert "код подключения" in body(response)
    assert "data:image/svg+xml" not in body(response)
    assert not SecureTOTPDevice.objects.filter(user=user).exists()


def test_first_login_with_enrollment_code(client, make_user):
    user = make_user("curator")
    code = issue_enrollment_code(None, user)
    login(client, user.username)

    # Шаг 1: код подключения. QR до него не показывается
    response = client.get(SETUP)
    assert "data:image/svg+xml" not in body(response)
    response = client.post(SETUP, {"enrollment_code": "AAAA-BBBB"})
    assert "Неверный или просроченный код" in body(response)
    response = client.post(SETUP, {"enrollment_code": code.lower().replace("-", " ")})
    assert response["Location"] == SETUP

    # Шаг 2: QR-код и код из приложения
    assert "data:image/svg+xml;base64," in body(client.get(SETUP))
    device = SecureTOTPDevice.objects.get(user=user, confirmed=False)
    response = client.post(SETUP, {"token": current_token(device)})
    assert response["Location"] == HOME
    device.refresh_from_db()
    user.refresh_from_db()
    assert device.confirmed
    assert not user.has_enrollment_code()  # код одноразовый
    assert AuditLog.objects.filter(user=user, action=AuditAction.TWO_FACTOR_ENABLED).exists()


def test_enrollment_code_expires(client, make_user):
    user = make_user("curator")
    issue_enrollment_code(None, user)
    User.objects.filter(pk=user.pk).update(otp_enrollment_expires=timezone.now())
    login(client, user.username)
    assert client.get(SETUP).status_code == 403


def test_totp_secret_encrypted_in_db(make_user):
    device = otp_device(make_user("curator"))
    with connection.cursor() as cursor:
        cursor.execute("SELECT key FROM accounts_securetotpdevice WHERE id = %s", [device.pk])
        stored = cursor.fetchone()[0]
    assert device.key not in stored
    assert len(bytes.fromhex(device.key)) == 20


def test_wrong_totp_rejected(client, make_user):
    user = make_user("curator")
    otp_device(user)
    login(client, user.username)
    response = client.post(VERIFY, {"token": "000000"})
    assert response.status_code == 200
    assert "Неверный код" in body(response)
    assert AuditLog.objects.filter(user=user, action=AuditAction.OTP_FAILED).exists()


def test_code_step_expires_after_5_minutes(client, make_user, monkeypatch):
    user = make_user("curator")
    device = otp_device(user)
    login(client, user.username)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + settings.OTP_PENDING_LOGIN_SECONDS + 1)
    response = client.post(VERIFY, {"token": current_token(device)})
    assert response["Location"] == LOGIN
    assert client.get(HOME)["Location"].startswith(LOGIN)


# --- Блокировка (НФТ-ИБ-03) ----------------------------------------------------------------


def test_nft_ib_03_lock_after_5_wrong_passwords(client, make_user):
    user = make_user("coordinator")
    for _ in range(4):
        assert "Неверный логин или пароль" in body(login(client, user.username, WRONG))
    assert "временно заблокирован" in body(login(client, user.username, WRONG))

    user.refresh_from_db()
    assert user.is_locked()
    assert user.locked_until - timezone.now() <= timedelta(minutes=15)
    assert AuditLog.objects.filter(user=user, action=AuditAction.ACCOUNT_LOCKED).count() == 1
    # Даже верный пароль не пускает, пока действует блокировка
    assert "временно заблокирован" in body(login(client, user.username))


def test_nft_ib_03_lock_expires_after_15_minutes(client, make_user):
    user = make_user("coordinator")
    User.objects.filter(pk=user.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
    assert login(client, user.username)["Location"] == VERIFY


def test_unknown_login_gets_same_answers(client, make_user):
    """По ответам нельзя понять, существует ли логин."""
    make_user("coordinator", username="real")
    answers = {
        name: [
            "временно заблокирован" in body(login(client, name, WRONG))
            for _ in range(settings.LOGIN_MAX_FAILED_ATTEMPTS + 1)
        ]
        for name in ("real", "ghost")
    }
    assert answers["real"] == answers["ghost"] == [False, False, False, False, True, True]


def test_unknown_login_hashes_password_like_known(client, make_user):
    """Время ответа не выдаёт существование логина: пароль хэшируется в обоих случаях."""
    with mock.patch("apps.accounts.services._burn_hash") as burn:
        login(client, "ghost", WRONG)
    burn.assert_called_once()


def test_typed_login_not_stored_in_audit(client):
    """В поле логина могут по ошибке ввести пароль — в журнал он не попадает."""
    login(client, "Мой-Секретный-Пароль", WRONG)
    log = AuditLog.objects.get(action=AuditAction.LOGIN_FAILED)
    assert log.username == ""
    assert "Секретный" not in str(log.extra)
    assert log.extra["login_sha256"] == lockout.login_hash("Мой-Секретный-Пароль")[:16]


def test_wrong_totp_counts_towards_lock(client, make_user):
    user = make_user("curator")
    otp_device(user)
    login(client, user.username)
    for _ in range(settings.LOGIN_MAX_FAILED_ATTEMPTS):
        response = client.post(VERIFY, {"token": "000000"})
    assert response["Location"] == LOGIN  # блокировка — выход из системы
    user.refresh_from_db()
    assert user.is_locked()
    assert client.get(HOME)["Location"].startswith(LOGIN)
    # Прерванный вход — не «выход»
    assert not AuditLog.objects.filter(user=user, action=AuditAction.LOGOUT).exists()


def test_locked_during_code_step_cannot_finish(client, make_user):
    """Сессия, открытая до блокировки, не может завершить вход верным кодом."""
    user = make_user("curator")
    device = otp_device(user)
    login(client, user.username)
    User.objects.filter(pk=user.pk).update(locked_until=timezone.now() + timedelta(minutes=15))
    response = client.post(VERIFY, {"token": current_token(device)})
    assert response["Location"] == LOGIN
    user.refresh_from_db()
    assert user.is_locked()  # верный код блокировку не снимает


def test_successful_login_resets_counter(client, make_user):
    user = make_user("coordinator")
    device = otp_device(user)
    for _ in range(settings.LOGIN_MAX_FAILED_ATTEMPTS - 1):
        login(client, user.username, WRONG)
    login(client, user.username)
    client.post(VERIFY, {"token": current_token(device)})
    client.post(reverse("accounts:logout"))
    assert "Неверный логин" in body(login(client, user.username, WRONG))


# --- Прочее --------------------------------------------------------------------------------


def test_login_case_insensitive(client, make_user):
    make_user("analyst", username="petrova")
    assert login(client, "PETROVA")["Location"] == VERIFY


def test_volunteer_without_staff_role_cannot_use_web(client, make_user):
    user = make_user("volunteer")
    response = login(client, user.username)
    assert response.status_code == 200
    assert "Telegram-бота" in body(response)


def test_blocked_user_cannot_login_and_is_logged_out(make_user, verified_client, client):
    user = make_user("coordinator")
    session_client = verified_client(user)
    assert session_client.get(HOME).status_code == 200
    user.is_active = False
    user.save()
    assert session_client.get(HOME).status_code == 302
    assert "Неверный логин или пароль" in body(login(client, user.username))


def test_logout_is_audited(make_user, verified_client):
    user = make_user("coordinator")
    client = verified_client(user)
    response = client.post(reverse("accounts:logout"))
    assert response["Location"] == LOGIN
    assert AuditLog.objects.filter(user=user, action=AuditAction.LOGOUT).exists()


def test_logout_requires_post(make_user, verified_client):
    client = verified_client(make_user("coordinator"))
    assert client.get(reverse("accounts:logout")).status_code == 405


def test_open_redirect_blocked(client, make_user):
    user = make_user("coordinator")
    device = otp_device(user)
    login(client, user.username, next="https://evil.example")
    assert client.post(VERIFY, {"token": current_token(device)})["Location"] == HOME


def test_next_preserved_through_2fa(client, make_user):
    user = make_user("coordinator")
    device = otp_device(user)
    login(client, user.username, next="/accounts/password/")
    response = client.post(VERIFY, {"token": current_token(device)})
    assert response["Location"] == "/accounts/password/"


def test_htmx_request_gets_hx_redirect(client, make_user):
    user = make_user("coordinator")
    otp_device(user)
    login(client, user.username)
    response = client.get(HOME, HTTP_HX_REQUEST="true")
    assert response.status_code == 204
    assert response["HX-Redirect"] == VERIFY


def test_nft_ib_04_session_timeout_settings():
    assert settings.SESSION_COOKIE_AGE == 30 * 60
    assert settings.SESSION_SAVE_EVERY_REQUEST is True


def test_nft_ib_03_password_policy():
    from config.settings import base

    assert base.PASSWORD_HASHERS[0].endswith("Argon2PasswordHasher")
    assert {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    } in base.AUTH_PASSWORD_VALIDATORS


def test_password_change(make_user, verified_client):
    user = make_user("coordinator")
    client = verified_client(user)
    new = "Совсем-новый-пароль-77"
    response = client.post(
        reverse("accounts:password_change"),
        {"old_password": PASSWORD, "new_password1": new, "new_password2": new},
    )
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.check_password(new)
    assert AuditLog.objects.filter(
        action=AuditAction.PASSWORD_CHANGED, object_id=str(user.pk)
    ).exists()


def test_short_password_rejected(make_user, verified_client):
    user = make_user("coordinator")
    response = verified_client(user).post(
        reverse("accounts:password_change"),
        {"old_password": PASSWORD, "new_password1": "Кор0ткий!", "new_password2": "Кор0ткий!"},
    )
    assert response.status_code == 200
    assert "12" in body(response)
