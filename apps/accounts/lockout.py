"""Блокировка после неудачных попыток входа (НФТ-ИБ-03).

Одинаково для существующих и несуществующих логинов: по поведению системы нельзя
понять, есть ли такой логин. Счётчик — атомарный инкремент в Redis (параллельные
запросы не теряют попытки). Срок блокировки существующего пользователя хранится
в БД (User.locked_until) — его видит и снимает администратор.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import User

# Попытки суммируются, пока между ними меньше 15 минут. Короткое окно не даёт заполнить
# Redis ключами по несуществующим логинам.
FAILURE_WINDOW = 15 * 60


def login_hash(username: str) -> str:
    """HMAC логина на секрете сервера — для ключей кэша и журнала аудита.

    Введённый «логин» может оказаться паролем: без секрета хэш не подобрать по словарю.
    """
    return salted_hmac("crm.login", username.strip().lower(), algorithm="sha256").hexdigest()


def _fail_key(username: str) -> str:
    return f"login-fail:{login_hash(username)}"


def _lock_key(username: str) -> str:
    return f"login-lock:{login_hash(username)}"


def _lock_seconds() -> int:
    return settings.LOGIN_LOCKOUT_MINUTES * 60


def is_locked(username: str, user: User | None) -> bool:
    if user is not None:
        return User.objects.filter(pk=user.pk, locked_until__gt=timezone.now()).exists()
    return bool(cache.get(_lock_key(username)))


def register_failure(username: str, user: User | None) -> bool:
    """Учесть неудачную попытку. True — если именно она привела к блокировке."""
    key = _fail_key(username)
    cache.add(key, 0, timeout=FAILURE_WINDOW)
    try:
        attempts = cache.incr(key)
    except ValueError:  # ключ истёк между add и incr
        cache.set(key, 1, timeout=FAILURE_WINDOW)
        attempts = 1
    cache.touch(key, FAILURE_WINDOW)
    if attempts < settings.LOGIN_MAX_FAILED_ATTEMPTS:
        return False

    cache.delete(key)
    if user is not None:
        until = timezone.now() + timedelta(seconds=_lock_seconds())
        User.objects.filter(pk=user.pk).update(locked_until=until)
        user.locked_until = until
    else:
        cache.set(_lock_key(username), 1, timeout=_lock_seconds())
    return True


def reset(username: str, user: User | None = None) -> None:
    """Успешный вход или разблокировка администратором."""
    cache.delete_many([_fail_key(username), _lock_key(username)])
    if user is not None and user.locked_until is not None:
        User.objects.filter(pk=user.pk).update(locked_until=None)
        user.locked_until = None
