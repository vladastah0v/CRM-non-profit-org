"""Сообщения волонтёрам через Telegram Bot API (ФТ-У-01, п. 6: отправляет Celery, не бот).

Бот и Celery используют один токен бота: кнопки в сообщении обрабатывает процесс бота.
Формат данных кнопки — `a:<действие>:<id назначения без дефисов>` (см. bot/callbacks.py).

В сообщения не попадают данные о здоровье и документах, а персональные данные
подопечного — никогда: адрес и телефон для визита бот показывает по кнопке «Подробнее»
(п. 7.2). В журнал не пишутся ни текст сообщения, ни Telegram ID.
"""

from __future__ import annotations

import enum
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 10
MAX_TEXT_LENGTH = 4096

Button = tuple[str, str]  # текст, данные кнопки


class SendResult(enum.Enum):
    SENT = "sent"
    # Пользователь заблокировал бота или удалил чат: повторять бессмысленно
    UNREACHABLE = "unreachable"
    # Сеть, перегрузка Telegram: можно повторить при следующем запуске
    FAILED = "failed"


def is_configured() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN)


def send_message(chat_id: int, text: str, buttons: list[list[Button]] | None = None) -> SendResult:
    if not is_configured():
        return SendResult.FAILED
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "text": text[:MAX_TEXT_LENGTH],
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row] for row in buttons
            ]
        }
    request = urllib.request.Request(  # noqa: S310 — адрес фиксированный, https
        API_URL.format(token=settings.TELEGRAM_BOT_TOKEN),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            response.read()
    except urllib.error.HTTPError as exc:
        # 400 «chat not found», 403 «bot was blocked by the user» — адресат недоступен
        if exc.code in (400, 403):
            logger.info("Telegram: адресат недоступен (HTTP %s)", exc.code)
            return SendResult.UNREACHABLE
        logger.warning("Telegram: ошибка отправки (HTTP %s)", exc.code)
        return SendResult.FAILED
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Telegram: нет связи (%s)", type(exc).__name__)
        return SendResult.FAILED
    return SendResult.SENT
