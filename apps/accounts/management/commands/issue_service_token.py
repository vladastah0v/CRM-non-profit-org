import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.accounts.models import TokenScope
from apps.accounts.tokens import issue_token
from apps.audit.models import AuditAction
from apps.audit.services import log_event


class Command(BaseCommand):
    help = (
        "Выпустить сервисный токен внутреннего API (бот, интеграции). Токен показывается "
        "один раз; в базе хранится только его хэш. Отзыв — «в архив» в админ-панели."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("name", help="Название, например «Telegram-бот»")
        parser.add_argument(
            "--scope",
            action="append",
            choices=TokenScope.values,
            required=True,
            help="Право токена; можно указать несколько раз",
        )
        parser.add_argument("--days", type=int, default=None, help="Срок действия в днях")

    def handle(self, *args: Any, **options: Any) -> None:
        days = options["days"]
        if days is not None and days <= 0:
            raise CommandError("Срок действия — положительное число дней")
        expires_at = timezone.now() + datetime.timedelta(days=days) if days else None
        token, raw = issue_token(options["name"], options["scope"], expires_at=expires_at)
        log_event(
            AuditAction.PERMISSION_CHANGE,
            obj=token,
            extra={"scopes": token.scopes, "event": "service_token_issued"},
        )
        self.stdout.write(f"Токен «{token.name}» ({', '.join(token.scopes)}): {raw}")
        self.stdout.write("Сохраните его сейчас: повторно показать токен нельзя.")
