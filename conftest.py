from collections.abc import Callable
from typing import Any

import pytest
from django.test import Client
from django_otp import DEVICE_ID_SESSION_KEY

from apps.accounts.models import SecureTOTPDevice

PASSWORD = "Надёжный-пароль-2026"


@pytest.fixture(autouse=True)
def _clear_cache():
    """Каждый тест начинается с пустым кэшем (счётчики блокировок, лимиты запросов)."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def make_user(db) -> Callable[..., Any]:
    from apps.accounts.models import Role, User, UserRole

    counter = {"n": 0}

    def factory(*roles: str, username: str | None = None, **fields: Any) -> User:
        counter["n"] += 1
        user = User.objects.create_user(
            username=username or f"user{counter['n']}",
            password=PASSWORD,
            last_name=fields.pop("last_name", "Иванова"),
            first_name=fields.pop("first_name", "Мария"),
            **fields,
        )
        for code in roles:
            UserRole.objects.create(user=user, role=Role.objects.get(code=code))
        return user

    return factory


def otp_device(user: Any) -> SecureTOTPDevice:
    device, _ = SecureTOTPDevice.objects.get_or_create(
        user=user, confirmed=True, defaults={"name": "test"}
    )
    return device


def current_token(device: SecureTOTPDevice) -> str:
    from django_otp.oath import totp

    return f"{totp(device.bin_key, device.step, device.t0, device.digits, device.drift):06d}"


@pytest.fixture
def verified_client(db) -> Callable[[Any], Client]:
    """Клиент, вошедший под пользователем и прошедший второй фактор."""

    def factory(user: Any) -> Client:
        client = Client()
        client.force_login(user)
        session = client.session
        session[DEVICE_ID_SESSION_KEY] = otp_device(user).persistent_id
        session.save()
        return client

    return factory
