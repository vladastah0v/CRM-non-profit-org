"""Файлы: проверка, шифрование, временные ссылки (ФТ-Д-05, НФТ-ИБ-11)."""

import time

import pytest
from django.core.exceptions import ValidationError
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.audit.models import AuditAction, AuditLog
from apps.consents.files import make_download_url, read_attachment, store_attachment
from apps.core.context import acting_as

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def upload(name: str, data: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data)


def test_stored_encrypted_and_readable(make_user):
    with acting_as(make_user("curator")):
        att = store_attachment(upload("Согласие Ивановой.pdf", PDF))
    assert att.content_type == "application/pdf" and att.size == len(PDF)
    with storages["attachments"].open(att.storage_key, "rb") as fh:
        on_disk = fh.read()
    assert PDF not in on_disk and b"%PDF" not in on_disk
    assert "Ивановой" not in att.storage_key
    assert read_attachment(att) == PDF


def test_type_checked_by_content_not_extension():
    with pytest.raises(ValidationError, match="Недопустимый тип"):
        store_attachment(upload("virus.pdf", b"MZ\x90\x00" + b"\x00" * 100))
    with pytest.raises(ValidationError, match="Недопустимый тип"):
        store_attachment(upload("notes.pdf", "просто текст".encode()))
    assert store_attachment(upload("photo.bin", PNG)).content_type == "image/png"


def test_size_limit(settings):
    settings.ATTACHMENT_MAX_SIZE = 100
    with pytest.raises(ValidationError, match="больше"):
        store_attachment(upload("big.pdf", PDF + b"0" * 200))
    with pytest.raises(ValidationError, match="пустой"):
        store_attachment(upload("empty.pdf", b""))


def test_download_by_signed_link(make_user, verified_client):
    user = make_user("curator")
    with acting_as(user):
        att = store_attachment(upload("Согласие.pdf", PDF))
    client = verified_client(user)
    response = client.get(make_download_url(att, user))
    assert response.status_code == 200
    assert response.content == PDF
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"].startswith("attachment;")
    assert "no-store" in response["Cache-Control"]
    assert AuditLog.objects.filter(action=AuditAction.FILE_DOWNLOAD, object_id=str(att.pk)).exists()


def test_link_is_personal(make_user, verified_client):
    owner = make_user("curator")
    other = make_user("admin")  # даже администратор не может пройти по чужой ссылке
    with acting_as(owner):
        att = store_attachment(upload("a.pdf", PDF))
    assert verified_client(other).get(make_download_url(att, owner)).status_code == 404


def test_no_access_without_rights(make_user, verified_client):
    owner = make_user("curator")
    stranger = make_user("curator")
    with acting_as(owner):
        att = store_attachment(upload("a.pdf", PDF))
    assert verified_client(stranger).get(make_download_url(att, stranger)).status_code == 404


def test_link_expires(make_user, verified_client, settings, monkeypatch):
    user = make_user("curator")
    with acting_as(user):
        att = store_attachment(upload("a.pdf", PDF))
    url = make_download_url(att, user)
    settings.ATTACHMENT_LINK_TTL = 300
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 301)
    assert verified_client(user).get(url).status_code == 410


def test_link_useless_after_role_removed(make_user, verified_client):
    from apps.accounts.models import UserRole

    user = make_user("curator")
    with acting_as(user):
        att = store_attachment(upload("a.pdf", PDF))
    client = verified_client(user)
    url = make_download_url(att, user)
    UserRole.objects.filter(user=user).delete()
    assert client.get(url).status_code == 403


def test_corrupted_file_gives_friendly_error(make_user, verified_client):
    user = make_user("curator")
    with acting_as(user):
        att = store_attachment(upload("a.pdf", PDF))
    storages["attachments"].delete(att.storage_key)
    response = verified_client(user).get(make_download_url(att, user))
    assert response.status_code == 500
    assert "Файл не удалось открыть" in response.content.decode()


def test_reencrypt_switches_key_then_removes_old_file(settings, django_capture_on_commit_callbacks):
    from apps.consents.files import reencrypt_attachment
    from apps.core import crypto

    key_a = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    key_b = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="
    settings.FIELD_ENCRYPTION_KEYS = f"old:{key_a}"
    att = store_attachment(upload("a.pdf", PDF))
    old_key = att.storage_key
    settings.FIELD_ENCRYPTION_KEYS = f"new:{key_b},old:{key_a}"
    with django_capture_on_commit_callbacks(execute=True):
        assert reencrypt_attachment(att) is True
    storage = storages["attachments"]
    assert att.storage_key != old_key and not storage.exists(old_key)
    with storage.open(att.storage_key, "rb") as fh:
        assert crypto.key_id_of(fh.read()) == "new"
    assert reencrypt_attachment(att) is False  # повторно — ничего не делает
    settings.FIELD_ENCRYPTION_KEYS = f"new:{key_b}"
    assert read_attachment(att) == PDF


def test_forged_token_rejected(make_user, verified_client):
    client = verified_client(make_user("admin"))
    assert client.get("/files/forged-token/").status_code == 404


def test_anonymous_redirected_to_login(make_user, client):
    user = make_user("curator")
    with acting_as(user):
        att = store_attachment(upload("a.pdf", PDF))
    response = client.get(make_download_url(att, user))
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]
