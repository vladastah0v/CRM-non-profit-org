"""ФТ-А-04, ФТ-Д-03: обезличивание удаляет ПДн, но сохраняет обезличенные факты помощи
в статистике (сценарий 9.2); запрос на обезличивание после отзыва согласия."""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditAction, AuditLog
from apps.beneficiaries import services as beneficiary_services
from apps.beneficiaries.models import Beneficiary, BeneficiarySensitive
from apps.beneficiaries.tests.conftest import give_consent as give_beneficiary_consent
from apps.beneficiaries.tests.conftest import to_in_work
from apps.consents import anonymize
from apps.consents.models import Attachment, Consent, ConsentKind
from apps.consents.services import revoke_consent
from apps.interactions.models import Interaction
from apps.notifications.models import Notification, NotificationKind
from apps.reports import services as reports
from apps.requests import services as request_services
from apps.requests.models import HelpRequest
from apps.requests.tests.conftest import assistance_data
from apps.shifts import services as shift_services
from apps.shifts.models import AssignmentStatus
from apps.volunteers import services as volunteer_services
from apps.volunteers.models import Volunteer
from apps.volunteers.tests.conftest import give_consent as give_volunteer_consent

pytestmark = pytest.mark.django_db
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
TODAY = timezone.localdate


def pdf(name="scan.pdf"):
    return SimpleUploadedFile(name, PDF)


# ---------------------------------------------------------------------------
# Волонтёр
# ---------------------------------------------------------------------------
def test_fta04_volunteer_personal_data_erased_stats_kept(make_active, make_shift, refs):
    volunteer = make_active(email="anna@example.org", telegram_username="anna_p")
    original = volunteer.last_name
    Volunteer.objects.filter(pk=volunteer.pk).update(telegram_id=4242)
    volunteer.refresh_from_db()
    scan_consent = give_volunteer_consent(volunteer, scan=pdf())
    shift = make_shift(starts_in=-datetime.timedelta(days=2))
    assignment = shift_services.assign_to_shift(shift, volunteer)
    shift_services.change_assignment_status(assignment, AssignmentStatus.ATTENDED)
    volunteer_services.add_manual_hours(
        volunteer, worked_on=TODAY(), hours=Decimal(2), comment="Звонила Анне Петровой"
    )
    Interaction.objects.create(
        volunteer=volunteer, kind="call", happened_at=timezone.now(), text="Анна просила"
    )
    hours_before = volunteer_services.total_hours(volunteer)

    anonymize.anonymize_volunteer(volunteer, basis="request", document="вх. 15 от 08.10.2026")
    volunteer.refresh_from_db()
    assert volunteer.last_name == "Обезличено" and volunteer.first_name != "Анна"
    assert volunteer.birth_date == datetime.date(1995, 1, 1)  # остаётся только год
    assert (volunteer.phone, volunteer.email, volunteer.telegram_username) == ("", "", "")
    assert volunteer.telegram_id is None and volunteer.is_archived and volunteer.anonymized_at
    # Файлы удалены, согласие осталось как факт
    scan_consent.refresh_from_db()
    assert scan_consent.scan is None and not Attachment.objects.exists()
    # Копий ФИО в истории нет — только запись о самом обезличивании
    assert not Volunteer.history.filter(last_name=original).exists()
    assert Volunteer.history.filter(id=volunteer.pk).count() == 1
    assert not Interaction.objects.filter(text__contains="Анна").exists()
    assert volunteer.manual_hours.get().comment == "Обезличено"
    # Статистика: часы и отметки явки сохранены
    assert volunteer_services.total_hours(volunteer) == hours_before
    log = AuditLog.objects.get(action=AuditAction.ANONYMIZE)
    assert log.extra == {"basis": "request", "document": "вх. 15 от 08.10.2026"}
    # Вернуть из архива нельзя, повторно обезличить — тоже
    with pytest.raises(ValidationError):
        volunteer_services.restore_volunteer(volunteer)
    with pytest.raises(ValidationError):
        anonymize.anonymize_volunteer(volunteer, basis="request", document="1")


def test_anonymized_volunteer_not_active_in_later_reports(make_active, make_user):
    volunteer = make_active()
    period = reports.Period(TODAY() - datetime.timedelta(days=5), TODAY())
    assert reports.volunteer_report(make_user("admin"), period).active_at_end == 1
    anonymize.anonymize_volunteer(volunteer, basis="retention", document="приказ 3")
    assert reports.volunteer_report(make_user("admin"), period).active_at_end == 0


# ---------------------------------------------------------------------------
# Подопечный (сценарий 9.2)
# ---------------------------------------------------------------------------
def test_92_beneficiary_erased_but_assistance_kept_in_stats(
    make_beneficiary, curator, food, brefs, make_user
):
    beneficiary = make_beneficiary(phone="+79031234567")
    original = beneficiary.last_name
    give_beneficiary_consent(beneficiary, scan=pdf())
    give_beneficiary_consent(beneficiary, kind=ConsentKind.SPECIAL)
    to_in_work(beneficiary)
    beneficiary_services.save_sensitive(beneficiary, {"health": "Диабет", "snils": ""})
    beneficiary_services.add_document(beneficiary, kind="passport", upload=pdf("passport.pdf"))
    beneficiary_services.add_related_person(
        beneficiary, {"full_name": "Смирнова Ольга", "relation": "дочь", "phone": "+79035550000"}
    )
    help_request = request_services.create_request(
        beneficiary, {"assistance_type": food, "description": "Петру Смирнову нужны продукты"}
    )
    request_services.register_assistance(
        beneficiary,
        assistance_data(food, curator, program=brefs["program"], comment="Отдали лично Петру"),
        help_request=help_request,
    )
    period = reports.Period(TODAY(), TODAY())
    before = reports.program_report(make_user("admin"), period, brefs["program"])

    anonymize.anonymize_beneficiary(beneficiary, basis="revocation", document="отзыв от 08.10")
    beneficiary.refresh_from_db()
    assert beneficiary.full_name.startswith("Обезличено")
    assert (beneficiary.address, beneficiary.phone) == ("", "")
    assert not BeneficiarySensitive.objects.exists()
    assert not BeneficiarySensitive.history.exists()
    assert not beneficiary.documents.exists() and not beneficiary.related_persons.exists()
    assert not Attachment.objects.exists()
    assert not Beneficiary.history.filter(last_name=original).exists()
    help_request.refresh_from_db()
    assert "Петру" not in help_request.description
    assert not HelpRequest.history.filter(description__contains="Петру").exists()
    assert beneficiary.assistance.get().comment == ""
    # Обезличенные факты помощи остаются в отчёте по программе
    after = reports.program_report(make_user("admin"), period, brefs["program"])
    assert (after.beneficiaries, after.facts, after.by_type) == (
        before.beneficiaries,
        before.facts,
        before.by_type,
    )


# ---------------------------------------------------------------------------
# ФТ-Д-03: запрос после отзыва согласия
# ---------------------------------------------------------------------------
def test_ftd03_revocation_of_last_consent_requests_erasure(make_beneficiary, make_user):
    admin = make_user("admin")
    beneficiary = make_beneficiary()
    first = give_beneficiary_consent(beneficiary)
    second = give_beneficiary_consent(beneficiary)
    revoke_consent(first, revoked_on=TODAY())
    beneficiary.refresh_from_db()
    assert beneficiary.erasure_requested_on is None  # второе согласие действует
    revoke_consent(second, revoked_on=TODAY())
    beneficiary.refresh_from_db()
    assert beneficiary.erasure_requested_on == TODAY()
    assert beneficiary.erasure_deadline == TODAY() + datetime.timedelta(days=30)
    note = Notification.objects.filter(
        recipient=admin, kind=NotificationKind.CONSENT_REVOKED
    ).first()
    assert note is not None and f"{beneficiary.erasure_deadline:%d.%m.%Y}" in note.body
    assert anonymize.pending_requests() == [beneficiary]


def test_erasure_screens_admin_only(verified_client, make_user, make_beneficiary, coordinator):
    beneficiary = make_beneficiary()
    consent = give_beneficiary_consent(beneficiary)
    revoke_consent(consent, revoked_on=TODAY())
    url = reverse("consents:anonymize", args=["beneficiary", beneficiary.pk])
    assert verified_client(coordinator).get(url).status_code == 403
    assert verified_client(coordinator).get(reverse("consents:erasure_list")).status_code == 403

    admin = verified_client(make_user("admin"))
    listing = admin.get(reverse("consents:erasure_list")).content.decode()
    assert beneficiary.full_name in listing
    card = admin.get(beneficiary.get_absolute_url()).content.decode()
    assert "данные нужно обезличить до" in card
    # Без подтверждения и документа — нельзя
    response = admin.post(url, {"basis": "revocation", "document": ""})
    assert response.status_code == 200 and response.context["form"].errors
    admin.post(url, {"basis": "revocation", "document": "отзыв вх. 7", "confirm": "on"})
    beneficiary.refresh_from_db()
    assert beneficiary.anonymized_at is not None
    card = admin.get(beneficiary.get_absolute_url()).content.decode()
    assert "Данные обезличены" in card and "Вернуть из архива" not in card


def test_dismiss_erasure(verified_client, make_user, make_active):
    volunteer = make_active()
    consent = Consent.objects.get(volunteer=volunteer)
    revoke_consent(consent, revoked_on=TODAY())
    volunteer.refresh_from_db()
    assert volunteer.erasure_requested_on == TODAY()
    admin = verified_client(make_user("admin"))
    url = reverse("consents:dismiss_erasure", args=["volunteer", volunteer.pk])
    admin.post(url, {"reason": "Договор волонтёра до 2027 года"})
    volunteer.refresh_from_db()
    assert volunteer.erasure_requested_on is None
    log = AuditLog.objects.filter(extra__erasure="dismissed").get()
    assert log.object_id == str(volunteer.pk)
    assert (
        admin.get(reverse("consents:anonymize", args=["person", volunteer.pk])).status_code == 404
    )
