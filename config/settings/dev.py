"""Локальная разработка."""

from .base import *

DEBUG = True
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
# Бот обращается к API через внутренний вход Nginx (порт 8081), тот передаёт Host «web»
ALLOWED_HOSTS = [*ALLOWED_HOSTS, "web"]
# runserver создаёт поток на каждый запрос: постоянные соединения копились бы до лимита
# PostgreSQL (на сервере Gunicorn — фиксированное число потоков, там CONN_MAX_AGE полезен)
DATABASES["default"]["CONN_MAX_AGE"] = 0

# Письма в разработке — в консоль
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

LOGGING["loggers"]["apps"] = {"level": "DEBUG"}
