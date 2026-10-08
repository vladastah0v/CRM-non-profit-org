"""Нагрузочная проверка (этап 9): как на сервере — DEBUG выключен, Gunicorn, кэш шаблонов;
но без HTTPS (стенд внутри Docker). Только с отдельной базой *_load (scripts/load/run.sh)."""

from .base import *

DEBUG = False
ALLOWED_HOSTS = ["crm-web-load", "localhost", "127.0.0.1"]
