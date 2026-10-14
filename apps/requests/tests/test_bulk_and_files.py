"""ФТ-З-05: массовая регистрация помощи; ФТ-З-06: фото и документы к факту помощи."""

import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.audit.models import AuditAction, AuditLog
from apps.beneficiaries.models import Beneficiary
from apps.beneficiaries.tests.conftest import give_consent, to_in_work
from apps.consents import anonymize
from apps.consents.models import Attachment, ConsentKind
from apps.core.context import acting_as
from apps.core.models import CaseClosingReason
from apps.requests import services
from apps.requests.models import Assistance, AssistanceFile

from .conftest import TODAY, assistance_data

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
# Минимальный JPEG: определяется по сигнатуре
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def close_case(beneficiary: Beneficiary) -> None:
    Beneficiary.objects.filter(pk=beneficiary.pk).update(
        status="closed",
        closing_reason=CaseClosingReason.objects.create(name="Переезд"),
        outcome="Переехал",
    )


# ---------------------------------------------------------------------------
# ФТ-З-05
# ---------------------------------------------------------------------------
def test_bulk_creates_record_for_each(make_beneficiary, food, curator):
    people = [to_in_work(make_beneficiary()) for _ in range(3)]
    batch = uuid.uuid4()
    with acting_as(curator):
        created = services.register_assistance_bulk(
            people, assistance_data(food, curator, quantity=Decimal(2)), batch_id=batch
        )
    assert len(created) == 3
    assert {a.beneficiary_id for a in created} == {p.pk for p in people}
    assert all(a.quantity == 2 and a.unit == "шт." and a.batch_id == batch for a in created)
    # Автор и история — как при обычной регистрации
    assert all(a.created_by == curator for a in created)
    assert Assistance.history.filter(batch_id=batch).count() == 3
    logs = AuditLog.objects.filter(action=AuditAction.CREATE, object_type="requests.assistance")
    assert {log.extra["batch"] for log in logs} == {str(batch)}


def test_bulk_same_form_twice_is_not_duplicated(make_beneficiary, food, curator):
    people = [to_in_work(make_beneficiary())]
    batch = uuid.uuid4()
    services.register_assistance_bulk(people, assistance_data(food, curator), batch_id=batch)
    with pytest.raises(ValidationError, match="уже сохранена"):
        services.register_assistance_bulk(people, assistance_data(food, curator), batch_id=batch)
    assert Assistance.objects.count() == 1


def test_bulk_all_or_nothing(make_beneficiary, food, curator):
    ok = to_in_work(make_beneficiary())
    closed = to_in_work(make_beneficiary())
    close_case(closed)
    closed.refresh_from_db()
    with pytest.raises(ValidationError) as exc:
        services.register_assistance_bulk(
            [ok, closed], assistance_data(food, curator), batch_id=uuid.uuid4()
        )
    assert closed.full_name in str(exc.value)
    assert not Assistance.objects.exists()


def test_bulk_page_lists_only_own_open_cases(verified_client, make_user, make_beneficiary, curator):
    own = to_in_work(make_beneficiary())
    closed = to_in_work(make_beneficiary())
    close_case(closed)
    foreign = to_in_work(make_beneficiary(curator=make_user("curator")))
    content = verified_client(curator).get(reverse("requests:assistance_bulk")).content.decode()
    assert own.last_name in content
    assert closed.last_name not in content
    assert foreign.last_name not in content


def test_bulk_page_post(verified_client, make_beneficiary, food, curator):
    people = [to_in_work(make_beneficiary()) for _ in range(2)]
    other = to_in_work(make_beneficiary())
    client = verified_client(curator)
    url = reverse("requests:assistance_bulk")
    response = client.post(
        url,
        {
            "beneficiaries": [p.pk for p in people],
            "batch_id": str(uuid.uuid4()),
            "assistance_type": food.pk,
            "provided_on": TODAY().isoformat(),
            "quantity": "1",
            "executor_user": curator.pk,
        },
    )
    assert response.status_code == 302
    assert set(Assistance.objects.values_list("beneficiary", flat=True)) == {p.pk for p in people}
    assert other.pk not in set(Assistance.objects.values_list("beneficiary", flat=True))


def test_bulk_page_rejects_foreign_beneficiary(verified_client, make_user, make_beneficiary, food):
    curator = make_user("curator")
    foreign = to_in_work(make_beneficiary())  # подопечный другого куратора
    response = verified_client(curator).post(
        reverse("requests:assistance_bulk"),
        {
            "beneficiaries": [foreign.pk],
            "batch_id": str(uuid.uuid4()),
            "assistance_type": food.pk,
            "provided_on": TODAY().isoformat(),
            "quantity": "1",
            "executor_user": curator.pk,
        },
    )
    assert response.status_code == 200
    assert not Assistance.objects.exists()


@pytest.mark.parametrize(("role", "allowed"), [("coordinator", False), ("analyst", False)])
def test_bulk_page_roles(verified_client, make_user, role, allowed):
    status = verified_client(make_user(role)).get(reverse("requests:assistance_bulk")).status_code
    assert (status == 200) == allowed


def test_bulk_page_asks_to_narrow_large_list(verified_client, make_user, monkeypatch):
    from apps.requests import selectors

    monkeypatch.setattr(selectors, "BULK_LIMIT", 0)
    admin = make_user("admin")
    Beneficiary.objects.create(
        last_name="Многова", first_name="Анна", birth_date="1950-01-01", phone="+79030000999"
    )
    content = verified_client(admin).get(reverse("requests:assistance_bulk")).content.decode()
    assert "Уточните отбор" in content
    assert "Многова" not in content


# ---------------------------------------------------------------------------
# ФТ-З-06
# ---------------------------------------------------------------------------
@pytest.fixture
def assistance(make_beneficiary, food, curator):
    beneficiary = to_in_work(make_beneficiary())
    return services.register_assistance(beneficiary, assistance_data(food, curator))


def test_document_attached(assistance):
    item = services.add_assistance_file(
        assistance,
        kind="document",
        upload=SimpleUploadedFile("акт.pdf", PDF),
        title=" акт  выдачи ",
    )
    assert item.title == "акт выдачи"
    assert AuditLog.objects.filter(action=AuditAction.CREATE, object_id=str(item.pk)).exists()


def test_photo_needs_photo_consent(assistance):
    with pytest.raises(ValidationError, match="фото- и видеосъёмку"):
        services.add_assistance_file(
            assistance, kind="photo", upload=SimpleUploadedFile("фото.jpg", JPEG)
        )
    give_consent(assistance.beneficiary, kind=ConsentKind.PHOTO_VIDEO)
    item = services.add_assistance_file(
        assistance, kind="photo", upload=SimpleUploadedFile("фото.jpg", JPEG)
    )
    assert item.file.content_type == "image/jpeg"


def test_photo_must_be_an_image(assistance):
    give_consent(assistance.beneficiary, kind=ConsentKind.PHOTO_VIDEO)
    with pytest.raises(ValidationError):
        services.add_assistance_file(
            assistance, kind="photo", upload=SimpleUploadedFile("фото.pdf", PDF)
        )


def test_files_visible_only_to_managers(verified_client, make_user, assistance, curator):
    item = services.add_assistance_file(
        assistance, kind="document", upload=SimpleUploadedFile("акт.pdf", PDF)
    )
    files = Attachment.objects.filter(pk=item.file_id)
    assert files.visible_to(curator).exists()
    assert not files.visible_to(make_user("curator")).exists()
    assert not files.visible_to(make_user("coordinator")).exists()
    url = assistance.beneficiary.get_absolute_url()
    assert "Подтверждающий документ" in verified_client(curator).get(url).content.decode()
    coordinator_page = verified_client(make_user("coordinator")).get(url).content.decode()
    assert "Подтверждающий документ" not in coordinator_page


def test_file_pages(verified_client, assistance, curator):
    client = verified_client(curator)
    add_url = reverse("requests:assistance_file_add", args=[assistance.pk])
    assert client.get(add_url).status_code == 200
    response = client.post(
        add_url, {"kind": "document", "upload": SimpleUploadedFile("чек.pdf", PDF), "title": "чек"}
    )
    assert response.status_code == 302
    item = AssistanceFile.objects.get(assistance=assistance)
    archive_url = reverse("requests:assistance_file_archive", args=[assistance.pk, item.pk])
    assert client.post(archive_url).status_code == 302
    item.refresh_from_db()
    assert item.is_archived


def test_photo_without_consent_shows_error(verified_client, assistance, curator):
    response = verified_client(curator).post(
        reverse("requests:assistance_file_add", args=[assistance.pk]),
        {"kind": "photo", "upload": SimpleUploadedFile("фото.jpg", JPEG)},
    )
    assert response.status_code == 200
    assert "фото- и видеосъёмку" in response.content.decode()
    assert not AssistanceFile.objects.exists()


def test_anonymization_removes_files_keeps_facts(assistance):
    item = services.add_assistance_file(
        assistance, kind="document", upload=SimpleUploadedFile("акт.pdf", PDF)
    )
    anonymize.anonymize_beneficiary(
        assistance.beneficiary, basis="request", document="вх. 1 от 14.10.2026"
    )
    assert not AssistanceFile.objects.exists()
    assert not Attachment.objects.filter(pk=item.file_id).exists()
    assert Assistance.objects.filter(pk=assistance.pk).exists()
