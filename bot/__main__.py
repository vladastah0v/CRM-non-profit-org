"""Запуск: python -m bot. Режим long polling — боту не нужен входящий порт."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .config import Settings
from .crm import CrmClient
from .handlers import router

COMMANDS = [
    BotCommand(command="shifts", description="Мои смены и визиты"),
    BotCommand(command="signup", description="Записаться на смену"),
    BotCommand(command="help", description="Что умеет бот"),
    BotCommand(command="unlink", description="Отвязать Telegram"),
]


async def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Состояние диалога (ожидание числа часов) — в памяти процесса: у бота нет доступа к Redis
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    crm = CrmClient(settings.api_url, settings.api_token)
    bot = Bot(settings.bot_token)
    try:
        await bot.set_my_commands(COMMANDS)
        await dispatcher.start_polling(bot, crm=crm, allowed_updates=["message", "callback_query"])
    finally:
        await crm.close()


if __name__ == "__main__":
    asyncio.run(main())
