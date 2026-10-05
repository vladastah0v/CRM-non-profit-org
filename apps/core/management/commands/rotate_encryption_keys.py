"""Перешифровать все данные текущим ключом (ротация ключей, раздел 10.4 ТЗ)."""

from typing import Any

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import models, transaction
from django.db.models.functions import Cast

from apps.core import crypto
from apps.core.fields import EncryptedTextField


class Command(BaseCommand):
    help = "Перешифровать шифрованные поля и файлы текущим (первым) ключом FIELD_ENCRYPTION_KEYS."

    def handle(self, *args: Any, **options: Any) -> None:
        self.stdout.write(f"Текущий ключ: {crypto.get_keyring().primary_id}")
        for model in apps.get_models():
            fields = [
                f.name for f in model._meta.concrete_fields if isinstance(f, EncryptedTextField)
            ]
            if fields:
                count = self._rotate_model(model, fields)
                self.stdout.write(f"{model._meta.label}: перешифровано записей — {count}")

        from apps.consents.files import reencrypt_attachment
        from apps.consents.models import Attachment

        files = sum(reencrypt_attachment(a) for a in Attachment._base_manager.iterator())
        self.stdout.write(f"Файлы: перешифровано — {files}")
        self.stdout.write(
            self.style.SUCCESS(
                "Готово. Повторный запуск должен показать нули — тогда старый ключ "
                "можно убрать из окружения."
            )
        )

    @staticmethod
    def _rotate_model(model: type[models.Model], fields: list[str]) -> int:
        manager = model._base_manager
        # Сырой шифротекст (Cast обходит расшифровку) — чтобы понять, каким ключом он сделан
        raw = {f"raw_{f}": Cast(f, output_field=models.TextField()) for f in fields}
        count = 0
        for pk in manager.values_list("pk", flat=True).iterator(chunk_size=500):
            with transaction.atomic():
                # Блокировка строки: правка сотрудника не затрётся старым значением
                row = (
                    manager.select_for_update().filter(pk=pk).annotate(**raw).values(*raw, *fields)
                )
                current = row.first()
                if current is None:
                    continue
                stale = [
                    f
                    for f in fields
                    if current[f"raw_{f}"] and crypto.needs_rotation(current[f"raw_{f}"])
                ]
                if stale:
                    manager.filter(pk=pk).update(**{f: current[f] for f in stale})
                    count += 1
        return count
