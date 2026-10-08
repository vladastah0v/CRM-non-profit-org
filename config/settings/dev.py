"""Локальная разработка."""

from .base import *

DEBUG = True
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
ALLOWED_HOSTS = [*ALLOWED_HOSTS, "web"]  # бот обращается к API по внутренней сети Docker
# runserver создаёт поток на каждый запрос: постоянные соединения копились бы до лимита
# PostgreSQL (на сервере Gunicorn — фиксированное число потоков, там CONN_MAX_AGE полезен)
DATABASES["default"]["CONN_MAX_AGE"] = 0

# Письма в разработке — в консоль
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

LOGGING["loggers"]["apps"] = {"level": "DEBUG"}
