from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.crypto import generate_key


class Command(BaseCommand):
    help = "Сгенерировать ключ шифрования для FIELD_ENCRYPTION_KEYS (вывод в консоль, не в лог)."

    def handle(self, *args: Any, **options: Any) -> None:
        key_id = f"k{timezone.now():%Y%m%d}"
        self.stdout.write(f"{key_id}:{generate_key()}")
        self.stderr.write(
            "Новый ключ ставится ПЕРВЫМ в FIELD_ENCRYPTION_KEYS, старые остаются после него.\n"
            "После перезапуска выполните: manage.py rotate_encryption_keys"
        )
