"""Только для разработки: 2FA без телефона.

Подключает пользователю устройство TOTP без QR-кода и печатает текущий код входа.
Вход в браузере остаётся настоящим (пароль + код), меняется лишь источник кода.
При DEBUG=False команда не работает: на сервере 2FA подключается только с телефона.
"""

import time
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django_otp.oath import totp

from apps.accounts import services
from apps.accounts.models import SecureTOTPDevice, User
from apps.audit.models import AuditAction
from apps.audit.services import log_event

DEV_DEVICE_NAME = "Разработка (без телефона)"


class Command(BaseCommand):
    help = "Разработка: подключить 2FA без телефона и показать текущий код входа."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("username")

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG:
            raise CommandError("Команда только для разработки (DEBUG=True).")
        user = User.objects.filter(username__iexact=options["username"]).first()
        if user is None:
            raise CommandError("Пользователь не найден")

        device = services.get_confirmed_device(user)
        if device is None:
            SecureTOTPDevice.objects.filter(user=user).delete()  # незавершённые подключения
            device = SecureTOTPDevice.objects.create(
                user=user, name=DEV_DEVICE_NAME, confirmed=True
            )
            services.consume_enrollment_code(user)
            log_event(AuditAction.TWO_FACTOR_ENABLED, user=user, obj=user, extra={"dev": True})
            self.stdout.write(f"2FA для «{user.username}» подключена без телефона.")

        now = int(time.time())
        code = totp(device.bin_key, device.step, device.t0, device.digits, device.drift)
        left = device.step - (now - device.t0) % device.step
        self.stdout.write(f"Код входа: {code:0{device.digits}d} (действует ещё {left} с)")
