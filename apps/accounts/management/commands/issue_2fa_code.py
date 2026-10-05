from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.accounts.models import User
from apps.accounts.services import issue_enrollment_code


class Command(BaseCommand):
    help = (
        "Выдать одноразовый код подключения 2FA (первый администратор или восстановление "
        "доступа без админ-панели). Старые устройства пользователя удаляются."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("username")

    def handle(self, *args: Any, **options: Any) -> None:
        user = User.objects.filter(username__iexact=options["username"]).first()
        if user is None:
            raise CommandError("Пользователь не найден")
        code = issue_enrollment_code(None, user)
        self.stdout.write(
            f"Код подключения 2FA для «{user.username}»: {code} "
            f"(действует {settings.OTP_ENROLLMENT_TTL_HOURS} ч)"
        )
