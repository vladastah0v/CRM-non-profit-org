"""Шифрованное поле модели (НФТ-ИБ-05).

В базе хранится только шифротекст. Поиск и сортировка по таким полям невозможны —
поэтому шифруются лишь чувствительные данные (здоровье, документы и т. п.).

Шифротекст привязан к модели и полю (AAD): перенести значение из одного поля или
таблицы в другое без ключа нельзя — расшифровка не пройдёт проверку подлинности.
"""

from __future__ import annotations

from typing import Any

from django.db import models

from . import crypto


class EncryptedTextField(models.TextField):
    description = "Текст, зашифрованный AES-256-GCM"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("db_index") or kwargs.get("unique"):
            raise ValueError("Шифрованное поле нельзя индексировать")
        super().__init__(*args, **kwargs)

    @property
    def aad(self) -> bytes:
        # Исторические модели django-simple-history («HistoricalX») — свой AAD:
        # при сохранении истории значение шифруется заново
        return f"crm-field:{self.model._meta.label_lower}.{self.name}".encode()

    def from_db_value(self, value: str | None, expression: Any, connection: Any) -> str | None:
        if value is None or value == "":
            return value
        return crypto.decrypt_str(value, aad=self.aad)

    def get_prep_value(self, value: Any) -> str | None:
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return crypto.encrypt_str(str(value), aad=self.aad)

    def get_lookup(self, lookup_name: str) -> Any:
        # Шифротекст каждый раз разный, поэтому сравнение по значению молча ничего не найдёт.
        # Разрешена только проверка на NULL.
        if lookup_name != "isnull":
            raise TypeError(f"Поиск «{lookup_name}» по шифрованному полю невозможен")
        return super().get_lookup(lookup_name)
