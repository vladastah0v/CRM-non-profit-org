"""Документы волонтёра со сроком действия (ФТ-В-09)."""

import datetime

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditAction, AuditLog
from apps.consents.models import Attachment
from apps.core.models import VolunteerDocumentType
from apps.volunteers import services
from apps.volunteers.models import VolunteerDocument

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
TODAY = timezone.localdate()


def days(n: int) -> datetime.date:
    return TODAY + datetime.timedelta(days=n)


@pytest.fixture
def med_book(db) -> VolunteerDocumentType:
    return VolunteerDocumentType.objects.get(name="Личная медицинская книжка")


def test_seeded_document_types():
    names = set(VolunteerDocumentType.objects.values_list("name", flat=True))
    assert {"Личная медицинская книжка", "Справка об отсутствии судимости"} <= names


def test_add_document_with_scan(make_volunteer, med_book):
    volunteer = make_volunteer()
    document = services.add_document(
        volunteer,
        document_type=med_book,
        issued_on=days(-300),
        valid_until=days(65),
        scan=SimpleUploadedFile("медкнижка.pdf", PDF),
        comment="  проверена   на месте ",
    )
    assert document.scan is not None
    assert document.comment == "проверена на месте"
    assert AuditLog.objects.filter(action=AuditAction.CREATE, object_id=str(document.pk)).exists()


def test_valid_until_not_before_issue(make_volunteer, med_book):
    with pytest.raises(ValidationError):
        services.add_document(
            make_volunteer(), document_type=med_book, issued_on=days(-1), valid_until=days(-2)
        )


def test_archived_card_is_read_only(make_volunteer, med_book):
    volunteer = make_volunteer()
    services.archive_volunteer(volunteer)
    with pytest.raises(ValidationError):
        services.add_document(volunteer, document_type=med_book, valid_until=days(30))


def test_current_document_is_the_longest_valid(make_volunteer, med_book):
    volunteer = make_volunteer()
    old = services.add_document(volunteer, document_type=med_book, valid_until=days(10))
    new = services.add_document(volunteer, document_type=med_book, valid_until=days(375))
    # Старую запись внесли позже — всё равно действует та, что дольше
    late_old = services.add_document(volunteer, document_type=med_book, valid_until=days(5))
    current = set(VolunteerDocument.objects.current().values_list("pk", flat=True))
    assert current == {new.pk}
    assert old.state(current=False) == "заменён"
    assert late_old.pk not in current


def test_unlimited_document_wins_and_types_are_separate(make_volunteer, med_book):
    volunteer = make_volunteer()
    certificate = VolunteerDocumentType.objects.get(name="Справка об отсутствии судимости")
    dated = services.add_document(volunteer, document_type=med_book, valid_until=days(375))
    unlimited = services.add_document(volunteer, document_type=med_book)
    other = services.add_document(volunteer, document_type=certificate, valid_until=days(3))
    current = set(VolunteerDocument.objects.current().values_list("pk", flat=True))
    assert current == {unlimited.pk, other.pk}
    assert dated.pk not in current


def test_archived_document_does_not_replace(make_volunteer, med_book):
    volunteer = make_volunteer()
    first = services.add_document(volunteer, document_type=med_book, valid_until=days(10))
    wrong = services.add_document(volunteer, document_type=med_book, valid_until=days(900))
    services.archive_document(wrong)
    assert set(VolunteerDocument.objects.current().values_list("pk", flat=True)) == {first.pk}
    assert AuditLog.objects.filter(action=AuditAction.ARCHIVE, object_id=str(wrong.pk)).exists()


@pytest.mark.parametrize(
    ("valid_until", "state"),
    [(None, "действует"), (31, "действует"), (30, "истекает"), (0, "истекает"), (-1, "истёк")],
)
def test_state(make_volunteer, med_book, valid_until, state):
    until = None if valid_until is None else days(valid_until)
    document = services.add_document(make_volunteer(), document_type=med_book, valid_until=until)
    assert document.state(current=True) == state


# --- Карточка и права ---


def test_card_shows_documents_to_coordinator(make_user, verified_client, make_volunteer, med_book):
    volunteer = make_volunteer()
    services.add_document(
        volunteer,
        document_type=med_book,
        valid_until=days(20),
        scan=SimpleUploadedFile("медкнижка.pdf", PDF),
    )
    response = verified_client(make_user("coordinator")).get(volunteer.get_absolute_url())
    content = response.content.decode()
    assert "Личная медицинская книжка" in content
    assert "истекает" in content
    assert "Скан" in content
    assert reverse("volunteers:document_add", args=[volunteer.pk]) in content


def test_curator_does_not_see_documents(make_user, verified_client, make_volunteer, med_book):
    volunteer = make_volunteer()
    document = services.add_document(
        volunteer, document_type=med_book, scan=SimpleUploadedFile("медкнижка.pdf", PDF)
    )
    curator = make_user("curator")
    response = verified_client(curator).get(volunteer.get_absolute_url())
    assert response.status_code == 200
    assert "Личная медицинская книжка" not in response.content.decode()
    assert document.scan is not None
    scans = Attachment.objects.filter(pk=document.scan.pk)
    assert not scans.visible_to(curator).exists()
    assert scans.visible_to(make_user("coordinator")).exists()


def test_add_document_page(make_user, verified_client, make_volunteer, med_book):
    volunteer = make_volunteer()
    client = verified_client(make_user("coordinator"))
    url = reverse("volunteers:document_add", args=[volunteer.pk])
    assert client.get(url).status_code == 200
    response = client.post(
        url,
        {
            "document_type": med_book.pk,
            "issued_on": days(-10).isoformat(),
            "valid_until": days(355).isoformat(),
            "scan": SimpleUploadedFile("медкнижка.pdf", PDF),
        },
    )
    assert response.status_code == 302
    document = VolunteerDocument.objects.get(volunteer=volunteer)
    assert document.valid_until == days(355)
    assert document.scan is not None


def test_add_document_form_checks_dates(make_user, verified_client, make_volunteer, med_book):
    volunteer = make_volunteer()
    client = verified_client(make_user("coordinator"))
    response = client.post(
        reverse("volunteers:document_add", args=[volunteer.pk]),
        {
            "document_type": med_book.pk,
            "issued_on": days(-1).isoformat(),
            "valid_until": days(-5).isoformat(),
        },
    )
    assert response.status_code == 200
    assert "раньше даты выдачи" in response.content.decode()
    assert not VolunteerDocument.objects.exists()


def test_archive_document_page(make_user, verified_client, make_volunteer, med_book):
    volunteer = make_volunteer()
    document = services.add_document(volunteer, document_type=med_book, valid_until=days(5))
    client = verified_client(make_user("coordinator"))
    url = reverse("volunteers:document_archive", args=[volunteer.pk, document.pk])
    assert client.get(url).status_code == 405
    assert client.post(url).status_code == 302
    document.refresh_from_db()
    assert document.is_archived


def test_archive_document_of_other_volunteer_is_404(
    make_user, verified_client, make_volunteer, med_book
):
    document = services.add_document(make_volunteer(), document_type=med_book)
    other = make_volunteer()
    client = verified_client(make_user("coordinator"))
    response = client.post(reverse("volunteers:document_archive", args=[other.pk, document.pk]))
    assert response.status_code == 404
