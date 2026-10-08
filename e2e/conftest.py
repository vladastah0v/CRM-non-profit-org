"""Сквозные тесты в браузере (раздел 9.1 ТЗ: Playwright, ключевые сценарии перед релизом).

Запуск — scripts/e2e/run.sh: отдельная база *_e2e, приложение и контейнер Playwright.
Вход настоящий: пароль + код TOTP, который тест вычисляет из известного секрета.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import os
import struct
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import ConsoleMessage, Page

BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:8000")
PASSWORD = os.environ.get("E2E_PASSWORD", "E2e-проверка-2026")
TOTP_KEY = bytes.fromhex(os.environ.get("E2E_TOTP_KEY", "3132333435363738393031323334353637383930"))
SHOTS = Path(os.environ.get("E2E_SCREENSHOTS", "/mnt/out/e2e"))
ACCOUNTS: dict[str, Iterator[int]] = {}


def totp(at: float | None = None) -> str:
    """RFC 6238: HMAC-SHA1, шаг 30 с, 6 цифр — как у приложения-аутентификатора."""
    counter = int((at or time.time()) // 30)
    digest = hmac.new(TOTP_KEY, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def login(page: Page, role: str) -> None:
    """Вход под ещё не использованной учётной записью роли (код TOTP одноразовый)."""
    number = next(ACCOUNTS.setdefault(role, itertools.count()))
    page.goto(f"{BASE_URL}/accounts/login/")
    page.get_by_label("Логин").fill(f"e2e-{role}-{number}")
    page.get_by_label("Пароль").fill(PASSWORD)
    page.get_by_role("button", name="Продолжить").click()
    page.get_by_label("Код из приложения").fill(totp())
    page.get_by_role("button", name="Войти", exact=True).click()
    page.wait_for_url(f"{BASE_URL}/")


@pytest.fixture
def errors(page: Page) -> list[str]:
    """Ошибки в консоли браузера — в том числе нарушения CSP (встроенные скрипты и стили)."""
    found: list[str] = []

    def collect(message: ConsoleMessage) -> None:
        # На стенде без HTTPS браузер игнорирует заголовок COOP и пишет об этом — не ошибка
        if message.type == "error" and "Cross-Origin-Opener-Policy" not in message.text:
            found.append(message.text)

    page.on("console", collect)
    page.on("pageerror", lambda exc: found.append(str(exc)))
    return found


def shot(page: Page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def no_horizontal_scroll(page: Page) -> bool:
    return bool(
        page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    )
