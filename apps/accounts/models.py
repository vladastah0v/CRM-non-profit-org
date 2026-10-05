import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Сотрудник фонда. Первичный ключ — UUID (п. 4.1 ТЗ)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    middle_name = models.CharField("отчество", max_length=150, blank=True)

    class Meta:
        verbose_name = "пользователь"
        verbose_name_plural = "пользователи"
        ordering = ["last_name", "first_name"]

    def __str__(self) -> str:
        return self.full_name or self.username

    @property
    def full_name(self) -> str:
        parts = (self.last_name, self.first_name, self.middle_name)
        return " ".join(p for p in parts if p)
