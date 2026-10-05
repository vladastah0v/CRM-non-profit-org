from typing import Any, Self

from django.db import models

from apps.accounts.roles import RoleCode, user_has_role
from apps.core.fields import EncryptedTextField
from apps.core.models import ArchivableQuerySet, BaseModel


class AttachmentQuerySet(ArchivableQuerySet):
    def visible_to(self, user: Any) -> Self:
        """Администратор видит все файлы, остальные — загруженные ими.

        Доступ через карточки (волонтёр, подопечный) добавляется вместе с модулями.
        """
        if user_has_role(user, RoleCode.ADMIN):
            return self.all()
        if not getattr(user, "is_authenticated", False):
            return self.none()
        return self.filter(created_by=user)


class Attachment(BaseModel):
    """Файл: скан согласия, документ, фото. Содержимое хранится зашифрованным (ФТ-Д-05).

    Имя файла тоже шифруется: в нём часто бывает ФИО («Иванова_паспорт.pdf»).
    В хранилище объект лежит под случайным ключом без персональных данных.
    """

    original_name = EncryptedTextField("имя файла")
    content_type = models.CharField("тип", max_length=100)
    size = models.PositiveIntegerField("размер, байт")
    sha256 = models.CharField("хэш SHA-256", max_length=64)
    storage_key = models.CharField("ключ в хранилище", max_length=255, unique=True, editable=False)

    objects = AttachmentQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        verbose_name = "файл"
        verbose_name_plural = "файлы"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Файл {self.content_type}, {self.size} байт"
