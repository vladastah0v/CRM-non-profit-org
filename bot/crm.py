"""Клиент внутреннего API CRM для бота (/api/v1/bot/)."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=15)


class CrmError(Exception):
    """Ответ CRM с понятным волонтёру текстом (правило не выполнено и т. п.)."""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


class NotLinkedError(CrmError):
    """Telegram не привязан к карточке волонтёра (или волонтёр выбыл)."""


class CrmUnavailableError(CrmError):
    """CRM недоступна или ответила ошибкой сервера — попробовать позже."""


class CrmClient:
    def __init__(self, base_url: str, token: str, session: aiohttp.ClientSession | None = None):
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self._token = token
        self._session = session
        self._own_session = session is None

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT)
        return self._session

    async def _request(
        self, method: str, path: str, telegram_id: int, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-Telegram-User-Id": str(telegram_id),
            "Accept": "application/json",
        }
        try:
            async with self._get_session().request(
                method, self.base_url + path, json=payload, headers=headers
            ) as response:
                status = response.status
                try:
                    data = await response.json(content_type=None)
                except ValueError:
                    data = None
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.warning("CRM недоступна: %s", type(exc).__name__)
            raise CrmUnavailableError("CRM временно недоступна.", 503) from exc

        if not isinstance(data, dict):
            data = {}
        detail = str(data.get("detail") or "")
        if status < 400:
            return data
        if status == 403 and data.get("code") == "not_linked":
            raise NotLinkedError(detail or "Telegram не привязан.", status)
        if status in (401, 403):
            # Неверный или отозванный токен бота — ошибка настройки, не волонтёра
            logger.error("CRM отклонила токен бота (HTTP %s)", status)
            raise CrmUnavailableError("Бот временно не работает.", status)
        if status == 404:
            raise CrmError("Не найдено — возможно, назначение уже изменилось.", status)
        if status == 429:
            raise CrmError("Слишком много запросов. Попробуйте через несколько минут.", status)
        if status >= 500:
            logger.warning("CRM ответила HTTP %s", status)
            raise CrmUnavailableError("CRM временно недоступна.", status)
        raise CrmError(detail or "Не получилось. Попробуйте ещё раз.", status)

    # --- привязка и профиль ---
    async def link(self, telegram_id: int, code: str) -> dict[str, Any]:
        return await self._request("POST", "link/", telegram_id, {"code": code})

    async def unlink(self, telegram_id: int) -> dict[str, Any]:
        return await self._request("POST", "unlink/", telegram_id)

    async def me(self, telegram_id: int) -> dict[str, Any]:
        return await self._request("GET", "me/", telegram_id)

    # --- назначения ---
    async def assignments(self, telegram_id: int) -> list[dict[str, Any]]:
        data = await self._request("GET", "assignments/", telegram_id)
        return list(data.get("results", []))

    async def assignment(self, telegram_id: int, pk: str) -> dict[str, Any]:
        return await self._request("GET", f"assignments/{pk}/", telegram_id)

    async def confirm(self, telegram_id: int, pk: str) -> dict[str, Any]:
        return await self._request("POST", f"assignments/{pk}/confirm/", telegram_id)

    async def decline(self, telegram_id: int, pk: str) -> dict[str, Any]:
        return await self._request("POST", f"assignments/{pk}/decline/", telegram_id)

    async def attendance(
        self, telegram_id: int, pk: str, *, attended: bool, hours: Decimal | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"attended": attended}
        if hours is not None:
            payload["hours"] = str(hours)
        return await self._request("POST", f"assignments/{pk}/attendance/", telegram_id, payload)

    # --- запись на смену ---
    async def open_shifts(self, telegram_id: int) -> dict[str, Any]:
        return await self._request("GET", "shifts/", telegram_id)

    async def signup(self, telegram_id: int, pk: str) -> dict[str, Any]:
        return await self._request("POST", f"shifts/{pk}/signup/", telegram_id)
