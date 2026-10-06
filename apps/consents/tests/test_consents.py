"""Согласия (ФТ-Д-01, ФТ-Д-03) и доступ к файлам карточек волонтёров."""

import datetime

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.consents.files import make_download_url, store_attachment
from apps.consents.models import Attachment, Consent, ConsentSigner
from apps.consents.services import create_volunteer_consent, revoke_consent
from apps.core.context import acting_as

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture
def volunteer(db):
    from apps.volunteers import services

    return services.create_volunteer(
        {
            "last_name": "Зайцева",
            "first_name": "Нина",
            "birth_date": datetime.date(1990, 1, 1),
            "phone": "+79161112233",
        }
    )


def test_ft_d_01_validity(volunteer):
    day = TODAY()
    consent = create_volunteer_consent(
        volunteer, signed_on=day - datetime.timedelta(days=10), valid_until=day
    )
    assert consent.is_valid_on(day) and consent.state == "действует"
    assert not consent.is_valid_on(day + datetime.timedelta(days=1))
    assert Consent.objects.valid_on(day).count() == 1
    assert Consent.objects.valid_on(day + datetime.timedelta(days=1)).count() == 0


def test_ft_d_01_dates_validated(volunteer):
    with pytest.raises(ValidationError):
        create_volunteer_consent(volunteer, signed_on=TODAY() + datetime.timedelta(days=1))
    with pytest.raises(ValidationError):
        create_volunteer_consent(
            volunteer, signed_on=TODAY(), valid_until=TODAY() - datetime.timedelta(days=1)
        )


def test_ft_d_03_revocation(volunteer):
    consent = create_volunteer_consent(volunteer, signed_on=TODAY() - datetime.timedelta(days=5))
    with pytest.raises(ValidationError):
        revoke_consent(consent, revoked_on=TODAY() - datetime.timedelta(days=6))
    revoke_consent(consent, revoked_on=TODAY(), note="Письменное заявление")
    assert consent.state == "отозвано" and not consent.is_valid_on()
    with pytest.raises(ValidationError, match="уже отозвано"):
        revoke_consent(consent, revoked_on=TODAY())


def test_database_constraints(volunteer):
    with pytest.raises(IntegrityError), transaction.atomic():
        Consent.objects.create(signed_on=TODAY())  # без карточки
    with pytest.raises(IntegrityError), transaction.atomic():
        Consent.objects.create(
            volunteer=volunteer, signed_on=TODAY(), signer=ConsentSigner.REPRESENTATIVE
        )


@pytest.mark.parametrize(
    ("role", "visible"),
    [("admin", True), ("coordinator", True), ("curator", False), ("analyst", False)],
)
def test_consents_visible_by_matrix(role, visible, volunteer, make_user):
    create_volunteer_consent(volunteer, signed_on=TODAY())
    assert Consent.objects.visible_to(make_user(role)).exists() is visible


def test_coordinator_can_download_scan_uploaded_by_colleague(volunteer, make_user, verified_client):
    colleague = make_user("coordinator")
    with acting_as(colleague):
        consent = create_volunteer_consent(
            volunteer, signed_on=TODAY(), scan=SimpleUploadedFile("с.pdf", PDF)
        )
    coordinator = make_user("coordinator")
    assert consent.scan is not None
    response = verified_client(coordinator).get(make_download_url(consent.scan, coordinator))
    assert response.status_code == 200 and response.content == PDF

    # Куратор не видит сканы согласий волонтёров
    curator = make_user("curator")
    assert verified_client(curator).get(make_download_url(consent.scan, curator)).status_code == 404


def test_coordinator_cannot_see_unrelated_files(make_user):
    other = make_user("curator")
    with acting_as(other):
        store_attachment(SimpleUploadedFile("a.pdf", PDF))
    assert not Attachment.objects.visible_to(make_user("coordinator")).exists()


def test_inline_ignored_for_pdf(volunteer, make_user, verified_client):
    user = make_user("coordinator")
    consent = create_volunteer_consent(
        volunteer, signed_on=TODAY(), scan=SimpleUploadedFile("с.pdf", PDF)
    )
    assert consent.scan is not None
    response = verified_client(user).get(make_download_url(consent.scan, user, inline=True))
    assert response["Content-Disposition"].startswith("attachment;")
