"""Тесты: PostgreSQL как в рабочем окружении, кэш в памяти, Celery синхронно."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("DATABASE_URL", "postgres://crm:crm@localhost:5432/crm")

from .base import *

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
DATABASES["default"]["CONN_MAX_AGE"] = 0

# Быстрый хэш только в тестах
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

MEDIA_ROOT = BASE_DIR / "tmp" / "test-media"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
LOGGING["root"]["level"] = "WARNING"
