"""Шифрование чувствительных данных ключом вне БД (НФТ-ИБ-05)."""

import base64
import io

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection

from apps.consents.files import read_attachment, store_attachment
from apps.consents.models import Attachment
from apps.core import crypto

KEY_A = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
KEY_B = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="


def test_roundtrip_and_random_nonce():
    a = crypto.encrypt_str("Паспорт 4510 123456")
    b = crypto.encrypt_str("Паспорт 4510 123456")
    assert a != b
    assert crypto.decrypt_str(a) == crypto.decrypt_str(b) == "Паспорт 4510 123456"


def test_tampering_detected():
    raw = bytearray(crypto.encrypt_bytes(b"secret"))
    raw[-1] ^= 1
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(bytes(raw))


def test_aad_binds_ciphertext():
    data = crypto.encrypt_bytes(b"secret", aad=b"record-1")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(data, aad=b"record-2")


def test_garbage_rejected():
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_str("не-шифротекст")


@pytest.mark.parametrize(
    "raw",
    ["", "k1", "k1:not-base64!", "k1:AAAA", f"k-1:{KEY_A}", f"k1:{KEY_A},k1:{KEY_B}"],
)
def test_bad_key_config(raw):
    with pytest.raises(ImproperlyConfigured):
        crypto.parse_keys(raw)


def test_rotation_old_key_still_decrypts(settings):
    settings.FIELD_ENCRYPTION_KEYS = f"old:{KEY_A}"
    token = crypto.encrypt_str("данные")
    settings.FIELD_ENCRYPTION_KEYS = f"new:{KEY_B},old:{KEY_A}"
    assert crypto.decrypt_str(token) == "данные"
    assert crypto.needs_rotation(token)
    settings.FIELD_ENCRYPTION_KEYS = f"new:{KEY_B}"
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_str(token)


@pytest.mark.django_db
def test_field_and_file_stored_encrypted_and_rotated(settings):
    settings.FIELD_ENCRYPTION_KEYS = f"old:{KEY_A}"
    pdf = b"%PDF-1.4\n%%EOF\n"
    att = store_attachment(SimpleUploadedFile("Иванова_паспорт.pdf", pdf))
    with connection.cursor() as cursor:
        cursor.execute("SELECT original_name FROM consents_attachment WHERE id = %s", [att.pk])
        stored = cursor.fetchone()[0]
    assert "Иванова" not in stored
    assert not crypto.needs_rotation(stored)

    settings.FIELD_ENCRYPTION_KEYS = f"new:{KEY_B},old:{KEY_A}"
    call_command("rotate_encryption_keys", stdout=io.StringIO())
    with connection.cursor() as cursor:
        cursor.execute("SELECT original_name FROM consents_attachment WHERE id = %s", [att.pk])
        rotated = cursor.fetchone()[0]
    assert crypto.key_id_of(base64.b64decode(rotated)) == "new"
    # Старый ключ убран — и поле, и файл читаются новым
    settings.FIELD_ENCRYPTION_KEYS = f"new:{KEY_B}"
    att = Attachment.objects.get(pk=att.pk)
    assert att.original_name == "Иванова_паспорт.pdf"
    assert read_attachment(att) == pdf


@pytest.mark.django_db
def test_encrypted_field_lookup_forbidden():
    with pytest.raises(TypeError):
        list(Attachment.objects.filter(**{"original_name__icontains": "Иван"}))
