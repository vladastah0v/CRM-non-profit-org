from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_url: str
    api_token: str
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        """Только переменные бота: ключи шифрования и пароль базы ему не передаются."""
        missing = [
            name for name in ("TELEGRAM_BOT_TOKEN", "CRM_API_TOKEN") if not os.environ.get(name)
        ]
        if missing:
            raise ConfigError("Не заданы переменные окружения: " + ", ".join(missing))
        api_url = os.environ.get("CRM_API_URL", "http://web:8000/api/v1/bot/")
        return cls(
            bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            api_url=api_url if api_url.endswith("/") else api_url + "/",
            api_token=os.environ["CRM_API_TOKEN"],
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
