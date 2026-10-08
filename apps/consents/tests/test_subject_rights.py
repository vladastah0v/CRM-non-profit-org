"""НФТ-ПД-05: выдача всех данных человека; НФТ-ПД-06: обезличивание по сроку хранения."""

import datetime
import io

import pytest
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.audit.models import AuditAction, AuditLog
from apps.beneficiaries import services as beneficiary_services
from apps.beneficiaries.models import Beneficiary
from apps.beneficiaries.tests.conftest import give_consent, to_in_work
from apps.consents import anonymize
from apps.consents.models import ConsentKind
from apps.notifications.models import Notification, NotificationKind
from apps.volunteers import services as volunteer_services
from apps.volunteers.models import Volunteer

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate


def sheets_of(response):
    workbook = load_workbook(io.BytesIO(response.content))
    return {ws.title: list(ws.iter_rows(values_only=True)) for ws in workbook.worksheets}


def test_ftpd05_beneficiary_all_data_export(verified_client, make_user, make_beneficiary, food):
    beneficiary = make_beneficiary(address="Москва, ул. Садовая, 3")
    give_consent(beneficiary)
    give_consent(beneficiary, kind=ConsentKind.SPECIAL)
    to_in_work(beneficiary)
    beneficiary_services.save_sensitive(beneficiary, {"health": "Диабет 2 типа"})
    beneficiary_services.add_related_person(
        beneficiary, {"full_name": "Смирнова Ольга", "relation": "дочь"}
    )
    admin = verified_client(make_user("admin"))
    url = reverse("consents:subject_export", args=["beneficiary", beneficiary.pk])
    sheets = sheets_of(admin.get(url))
    assert {
        "Карточка",
        "Защищённые данные",
        "Согласия",
        "Связанные лица",
        "Заявки",
        "Файлы",
    } <= set(sheets)
    card = dict(row for row in sheets["Карточка"][1:])
    assert card["Адрес"] == "Москва, ул. Садовая, 3"
    assert ("Здоровье", "Диабет 2 типа") in sheets["Защищённые данные"]
    assert len(sheets["Согласия"]) == 4  # заголовок + три согласия
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {AuditAction.EXPORT, AuditAction.VIEW_SENSITIVE} <= actions


def test_ftpd05_volunteer_export_admin_only(verified_client, make_user, make_active, coordinator):
    volunteer = make_active()
    url = reverse("consents:subject_export", args=["volunteer", volunteer.pk])
    assert verified_client(coordinator).get(url).status_code == 403
    response = verified_client(make_user("admin")).get(url)
    sheets = sheets_of(response)
    card = dict(row for row in sheets["Карточка"][1:])
    assert card["Фамилия"] == volunteer.last_name
    assert "Код" not in str(sheets)  # служебные коды привязки не выгружаются
    assert len(sheets["Обучение"]) == 2 and len(sheets["История статусов"]) == 5


def test_ftpd06_expired_records_anonymized_monthly(
    settings, make_user, make_active, refs, make_beneficiary, brefs
):
    admin = make_user("admin")
    settings.PD_RETENTION_DAYS = 365
    old_volunteer = make_active()
    volunteer_services.change_status(old_volunteer, "left", reason=refs["reason"])
    Volunteer.objects.filter(pk=old_volunteer.pk).update(
        left_on=TODAY() - datetime.timedelta(days=400)
    )
    recent = make_active()
    volunteer_services.change_status(recent, "left", reason=refs["reason"])
    closed = make_beneficiary()
    beneficiary_services.change_status(
        closed, "closed", reason=brefs["reason"], outcome="Помощь оказана"
    )
    Beneficiary.objects.filter(pk=closed.pk).update(
        closed_on=TODAY() - datetime.timedelta(days=500)
    )

    result = anonymize.anonymize_expired()
    assert result == {"volunteers": 1, "beneficiaries": 1, "errors": 0}
    old_volunteer.refresh_from_db()
    recent.refresh_from_db()
    closed.refresh_from_db()
    assert old_volunteer.anonymized_at and closed.anonymized_at and recent.anonymized_at is None
    log = AuditLog.objects.filter(action=AuditAction.ANONYMIZE).first()
    assert log is not None and log.extra["basis"] == "retention"
    report = Notification.objects.get(recipient=admin, kind=NotificationKind.RETENTION_REPORT)
    assert "волонтёров — 1" in report.body
    assert anonymize.anonymize_expired()["volunteers"] == 0  # повторно — нечего


def test_ftpd06_not_configured_reminds_admins(settings, make_user):
    admin = make_user("admin")
    settings.PD_RETENTION_DAYS = 0
    assert anonymize.anonymize_expired() == {"volunteers": 0, "beneficiaries": 0, "errors": 0}
    note = Notification.objects.get(recipient=admin)
    assert "PD_RETENTION_DAYS" in note.body
