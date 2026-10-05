"""Загрузка и выдача файлов (ФТ-Д-05, НФТ-ИБ-11).

- проверка типа по содержимому (а не по расширению) и размера — до 20 МБ;
- шифрование AES-256-GCM до записи в хранилище (диск или S3 в РФ);
- выдача только через приложение по временной подписанной ссылке, с проверкой прав
  и записью в журнал аудита.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import filetype
from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.urls import reverse

from apps.core import crypto

from .models import Attachment

ALLOWED_TYPES = {
    "application/pdf": "PDF",
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
    "image/heic": "HEIC",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "XLSX",
}
LINK_SALT = "consents.attachment-link"


def _storage() -> Any:
    return storages["attachments"]


def _aad(attachment_id: uuid.UUID) -> bytes:
    # Привязка шифротекста к записи: подменить файл одной карточки файлом другой нельзя
    return b"crm-file:" + str(attachment_id).encode()


def validate_upload(upload: UploadedFile) -> str:
    """Проверить файл и вернуть его MIME-тип."""
    max_size = settings.ATTACHMENT_MAX_SIZE
    if upload.size is None or upload.size > max_size:
        raise ValidationError(
            f"Файл больше {max_size // (1024 * 1024)} МБ. Уменьшите его или разделите на части."
        )
    if upload.size == 0:
        raise ValidationError("Файл пустой.")
    upload.seek(0)
    head = upload.read(8192)
    upload.seek(0)
    kind = filetype.guess(head)
    if kind is None or kind.mime not in ALLOWED_TYPES:
        allowed = ", ".join(sorted(set(ALLOWED_TYPES.values())))
        raise ValidationError(f"Недопустимый тип файла. Разрешены: {allowed}.")
    return kind.mime


def store_attachment(upload: UploadedFile) -> Attachment:
    content_type = validate_upload(upload)
    data = upload.read()
    attachment = Attachment(
        id=uuid.uuid4(),
        original_name=(upload.name or "file")[:255],
        content_type=content_type,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    attachment.storage_key = _storage().save(
        _object_key(attachment.id),
        ContentFile(crypto.encrypt_bytes(data, aad=_aad(attachment.id))),
    )
    attachment.save()
    return attachment


def read_attachment(attachment: Attachment) -> bytes:
    with _storage().open(attachment.storage_key, "rb") as fh:
        encrypted = fh.read()
    data = crypto.decrypt_bytes(encrypted, aad=_aad(attachment.id))
    if hashlib.sha256(data).hexdigest() != attachment.sha256:
        raise crypto.DecryptionError("Хэш файла не совпадает")
    return data


def _object_key(attachment_id: uuid.UUID) -> str:
    # Случайный суффикс: новая версия файла никогда не перезаписывает старую
    return f"attachments/{attachment_id.hex[:2]}/{attachment_id}-{uuid.uuid4().hex[:8]}.bin"


def reencrypt_attachment(attachment: Attachment) -> bool:
    """Перешифровать файл текущим ключом (ротация). True — если файл был перешифрован.

    Порядок исключает потерю файла при сбое: новый объект записывается под новым ключом,
    затем на него переключается запись в БД, и только после этого удаляется старый.
    """
    storage = _storage()
    old_key = attachment.storage_key
    with storage.open(old_key, "rb") as fh:
        encrypted = fh.read()
    if crypto.key_id_of(encrypted) == crypto.get_keyring().primary_id:
        return False
    data = crypto.decrypt_bytes(encrypted, aad=_aad(attachment.id))
    if hashlib.sha256(data).hexdigest() != attachment.sha256:
        raise crypto.DecryptionError("Хэш файла не совпадает — перешифровка остановлена")
    new_key = storage.save(
        _object_key(attachment.id),
        ContentFile(crypto.encrypt_bytes(data, aad=_aad(attachment.id))),
    )
    with transaction.atomic():
        Attachment._base_manager.filter(pk=attachment.pk).update(storage_key=new_key)
        transaction.on_commit(lambda: storage.delete(old_key))
    attachment.storage_key = new_key
    return True


def make_download_url(attachment: Attachment, user: Any) -> str:
    """Ссылка действует ATTACHMENT_LINK_TTL секунд и только для этого пользователя."""
    token = signing.dumps({"a": str(attachment.id), "u": str(user.pk)}, salt=LINK_SALT)
    return reverse("consents:download", kwargs={"token": token})


def parse_download_token(token: str) -> dict[str, str]:
    """Расшифровать токен ссылки. SignatureExpired / BadSignature — наружу."""
    return signing.loads(token, salt=LINK_SALT, max_age=settings.ATTACHMENT_LINK_TTL)
