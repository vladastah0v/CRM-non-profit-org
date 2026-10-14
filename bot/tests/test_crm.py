"""HTTP-клиент CRM на тестовом aiohttp-сервере: заголовки, разбор ошибок."""

import asyncio
from decimal import Decimal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.config import ConfigError, Settings
from bot.crm import CrmClient, CrmError, CrmUnavailableError, NotLinkedError


def run_with_server(routes, scenario):
    async def main():
        app = web.Application()
        app.add_routes(routes)
        async with TestServer(app) as server:
            client = CrmClient(str(server.make_url("/api/v1/bot")), "crm_secret")
            try:
                return await scenario(client)
            finally:
                await client.close()

    return asyncio.run(main())


def test_sends_token_and_telegram_id():
    seen = {}

    async def me(request):
        seen.update(request.headers)
        return web.json_response({"first_name": "Анна"})

    data = run_with_server([web.get("/api/v1/bot/me/", me)], lambda c: c.me(42))
    assert data == {"first_name": "Анна"}
    assert seen["Authorization"] == "Bearer crm_secret"
    assert seen["X-Telegram-User-Id"] == "42"


def test_attendance_payload():
    seen = {}

    async def attendance(request):
        seen.update(await request.json())
        return web.json_response({"status": "attended"})

    route = web.post("/api/v1/bot/assignments/abc/attendance/", attendance)
    run_with_server([route], lambda c: c.attendance(1, "abc", attended=True, hours=Decimal("2.5")))
    assert seen == {"attended": True, "hours": "2.5"}


@pytest.mark.parametrize(
    ("status", "body", "error", "detail"),
    [
        (403, {"detail": "нет", "code": "not_linked"}, NotLinkedError, "нет"),
        (401, {"detail": "Неверный токен"}, CrmUnavailableError, "Бот временно не работает."),
        (400, {"detail": "Смена уже началась."}, CrmError, "Смена уже началась."),
        (404, {"detail": "Not found"}, CrmError, "Не найдено"),
        (429, {}, CrmError, "Слишком много"),
        (502, None, CrmUnavailableError, "CRM временно недоступна."),
    ],
)
def test_error_mapping(status, body, error, detail):
    async def handler(request):
        if body is None:
            return web.Response(status=status, text="<html>Bad gateway</html>")
        return web.json_response(body, status=status)

    with pytest.raises(error) as exc:
        run_with_server([web.get("/api/v1/bot/me/", handler)], lambda c: c.me(1))
    assert exc.value.detail.startswith(detail)


def test_unreachable_server():
    async def scenario():
        client = CrmClient("http://127.0.0.1:9/api/v1/bot/", "t")
        try:
            await client.me(1)
        finally:
            await client.close()

    with pytest.raises(CrmUnavailableError):
        asyncio.run(scenario())


def test_settings_from_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CRM_API_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:abc")
    monkeypatch.setenv("CRM_API_TOKEN", "crm_x")
    monkeypatch.setenv("CRM_API_URL", "http://nginx:8081/api/v1/bot")
    settings = Settings.from_env()
    assert settings.api_url == "http://nginx:8081/api/v1/bot/"
