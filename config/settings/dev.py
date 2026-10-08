"""Локальная разработка."""

from .base import *

DEBUG = True
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
ALLOWED_HOSTS = [*ALLOWED_HOSTS, "web"]  # бот обращается к API по внутренней сети Docker

# Письма в разработке — в консоль
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

LOGGING["loggers"]["apps"] = {"level": "DEBUG"}
