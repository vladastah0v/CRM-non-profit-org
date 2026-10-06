"""Общие настройки. Все секреты и параметры окружения — из переменных среды (НФТ-ИБ-10)."""

from pathlib import Path
from typing import Any

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# ---------------------------------------------------------------------------
# Приложения
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "unfold.apps.BasicAppConfig",
    "unfold.contrib.filters",
    "apps.core.admin_config.CRMAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
]
THIRD_PARTY_APPS = [
    "rest_framework",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    "django_filters",
    "django_htmx",
    "simple_history",
    "django_otp",
    "django_otp.plugins.otp_totp",
]
LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.audit",
    "apps.consents",
    "apps.notifications",
    "apps.volunteers",
    "apps.beneficiaries",
]
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "apps.accounts.middleware.RequireOTPMiddleware",
    "apps.core.context.CurrentUserMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
]

ROOT_URLCONF = "config.urls"
# Сообщения — на сервере, а не в cookie: в них бывает код подключения 2FA
MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"
WSGI_APPLICATION = "config.wsgi.application"
ADMIN_URL = env("DJANGO_ADMIN_URL", default="admin/")

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.accounts.context_processors.roles",
                "apps.notifications.context_processors.notifications",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# База данных, кэш, очередь
# ---------------------------------------------------------------------------
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["ATOMIC_REQUESTS"] = True
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = env("REDIS_URL", default="redis://redis:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "crm",
    }
}

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://redis:6379/1")
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True
CELERY_TIMEZONE = env("TIME_ZONE", default="Europe/Moscow")
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BEAT_SCHEDULE: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Пользователи и пароли (НФТ-ИБ-03)
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:home"
LOGOUT_REDIRECT_URL = "accounts:login"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Блокировка после неудачных попыток входа (НФТ-ИБ-03)
LOGIN_MAX_FAILED_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15

# Двухфакторная аутентификация TOTP (НФТ-ИБ-02)
OTP_TOTP_ISSUER = env("OTP_TOTP_ISSUER", default="CRM НКО")
OTP_TOTP_THROTTLE_FACTOR = 1
OTP_ENROLLMENT_TTL_HOURS = 72  # срок действия кода подключения 2FA от администратора
OTP_PENDING_LOGIN_SECONDS = 5 * 60  # время на ввод кода после пароля
OTP_ENROLLMENT_LOGIN_SECONDS = 15 * 60  # первое подключение: установить приложение и QR

# Сессия: выход после 30 минут бездействия (НФТ-ИБ-04)
SESSION_COOKIE_AGE = 30 * 60
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"

# ---------------------------------------------------------------------------
# Локализация: русский язык, ДД.ММ.ГГГГ, часовой пояс фонда (п. 3.10)
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "ru"
LANGUAGES = [("ru", "Русский")]
TIME_ZONE = env("TIME_ZONE", default="Europe/Moscow")
USE_I18N = True
USE_TZ = True
FORMAT_MODULE_PATH = ["config.formats"]

# ---------------------------------------------------------------------------
# Статика и файлы. Файлы хранятся вне веб-корня (НФТ-ИБ-11).
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_ROOT = env.path("MEDIA_ROOT", default=str(BASE_DIR / "media"))
FILE_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024

ATTACHMENT_MAX_SIZE = 20 * 1024 * 1024  # НФТ-ИБ-11
ATTACHMENT_LINK_TTL = env.int("ATTACHMENT_LINK_TTL", default=300)  # срок жизни ссылки, сек

# Файлы шифруются приложением и выдаются только через него, поэтому хранилищу
# достаточно быть приватным: локальный диск в разработке, S3 в РФ на сервере.
S3_BUCKET = env("S3_BUCKET", default="")
if S3_BUCKET:
    ATTACHMENTS_STORAGE = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": S3_BUCKET,
            "endpoint_url": env("S3_ENDPOINT_URL"),
            "region_name": env("S3_REGION", default="ru-central1"),
            "access_key": env("S3_ACCESS_KEY"),
            "secret_key": env("S3_SECRET_KEY"),
            "default_acl": "private",
            "querystring_auth": True,
            "file_overwrite": False,
        },
    }
else:
    ATTACHMENTS_STORAGE = {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": str(MEDIA_ROOT), "base_url": None},
    }

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "attachments": ATTACHMENTS_STORAGE,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Ключи шифрования чувствительных полей и файлов — вне базы (НФТ-ИБ-05).
# Формат: «id:ключ,id:ключ»; первый — текущий. Сгенерировать: manage.py generate_encryption_key
FIELD_ENCRYPTION_KEYS = env("FIELD_ENCRYPTION_KEYS")

# ---------------------------------------------------------------------------
# Безопасность (общая часть; HTTPS-настройки — в prod.py)
# ---------------------------------------------------------------------------
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
CSRF_COOKIE_HTTPONLY = False  # HTMX читает токен из шаблона, не из cookie
CSRF_FAILURE_VIEW = "apps.core.views.csrf_failure"
# Брать IP клиента из X-Real-IP (его ставит Nginx). Включать только за доверенным прокси.
TRUST_X_REAL_IP = env.bool("TRUST_X_REAL_IP", default=False)

# ---------------------------------------------------------------------------
# API (п. 7.5)
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["apps.accounts.permissions.IsVerifiedStaff"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_RATES": {"intake": "10/min"},
}
# Приём анкет с сайта (п. 7.1): общий с сайтом секрет подписи HMAC-SHA256.
# Пусто — приём анкет выключен.
# Сгенерировать: python -c "import secrets; print(secrets.token_hex(32))"
INTAKE_HMAC_SECRET = env("INTAKE_HMAC_SECRET", default="")

SPECTACULAR_SETTINGS = {
    "TITLE": "CRM НКО — внутренний API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # Документация API — только сотрудникам (п. 7.5)
    "SERVE_PERMISSIONS": ["apps.accounts.permissions.IsVerifiedStaff"],
    "SCHEMA_PATH_PREFIX": "/api/v1/",
    # Swagger UI из локальной статики, без CDN — совместимо с CSP
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
}

# ---------------------------------------------------------------------------
# Админ-панель (Unfold)
# ---------------------------------------------------------------------------
UNFOLD = {
    "SITE_TITLE": "CRM НКО — администрирование",
    "SITE_HEADER": "CRM НКО",
    "SHOW_HISTORY": False,  # встроенный журнал админки отключён — см. CRMModelAdmin
    "SHOW_VIEW_ON_SITE": False,
}

# ---------------------------------------------------------------------------
# Журналирование: только в stdout, без персональных данных
# ---------------------------------------------------------------------------
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
    },
}

# ---------------------------------------------------------------------------
# Сбор ошибок: GlitchTip / Sentry self-hosted, без отправки ПДн (п. 6.2)
# ---------------------------------------------------------------------------
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    def _scrub_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
        """Из события уходят только тип ошибки и стек: без тела запроса, cookie и заголовков."""
        request = event.get("request") or {}
        for key in ("data", "cookies", "headers", "query_string", "env"):
            request.pop(key, None)
        event.pop("user", None)
        return event

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration(), CeleryIntegration()],
        send_default_pii=False,
        # Локальные переменные стека могут содержать ПДн и ключи шифрования
        include_local_variables=False,
        include_source_context=False,
        max_request_body_size="never",
        before_send=_scrub_event,  # type: ignore[arg-type]
        environment=env("SENTRY_ENVIRONMENT", default="production"),
        traces_sample_rate=0.0,
    )
