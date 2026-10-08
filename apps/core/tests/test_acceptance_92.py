# ruff: noqa: F811 — фикстуры модулей подключаются импортом
"""Приёмочные сценарии раздела 9.2 ТЗ — автоматическая часть протокола приёмки.

Каждый тест — один сценарий, через HTTP от имени нужной роли, как его пройдёт сотрудник.
Сценарии, которым нужен живой сервер или браузер, — в своих файлах (см.
docs/acceptance/protocol.md): бот — apps/shifts/tests/test_acceptance.py, 360 px —
e2e/, восстановление из копии — scripts/backup/restore-test.sh.
"""

from __future__ import annotations

import datetime
import io
import json
import time
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.audit.models import AuditAction, AuditLog
from apps.beneficiaries.models import Beneficiary, CaseStatus
from apps.beneficiaries.tests.conftest import (  # noqa: F401
    brefs,
    curator,
    give_consent,
    make_beneficiary,
    to_in_work,
)
from apps.core.models import AssistanceType
from apps.core.signatures import sign
from apps.notifications.models import Notification, NotificationKind
from apps.reports import services as reports
from apps.requests import services as request_services
from apps.requests.models import HelpRequest, RequestStatus
from apps.requests.tests.conftest import assistance_data
from apps.volunteers.models import Volunteer, VolunteerStatus
from apps.volunteers.tests.conftest import make_volunteer, refs  # noqa: F401
from conftest import PASSWORD, current_token, otp_device

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate


def post_intake(data: dict) -> int:
    body = json.dumps(data).encode()
    ts = str(int(time.time()))
    response = Client().post(
        reverse("api-v1:intake-volunteer"),
        body,
        content_type="application/json",
        HTTP_X_TIMESTAMP=ts,
        HTTP_X_SIGNATURE=sign("test-intake-secret", ts, body),
    )
    return response.status_code


def test_92_1_site_application_notifies_and_detects_duplicate(make_user, refs):
    """Анкета с сайта создаёт карточку волонтёра, координатор получает уведомление,
    дубль по телефону обнаруживается."""
    coordinator = make_user("coordinator")
    form = {
        "last_name": "Волкова",
        "first_name": "Ирина",
        "birth_date": "1990-04-20",
        "phone": "+7 926 000-11-22",
        "consent": True,
    }
    assert post_intake(form) == 201
    assert post_intake(form | {"last_name": "Волкова-Петрова", "first_name": "Ира"}) == 201
    first, second = Volunteer.objects.order_by("created_at")
    assert first.status == VolunteerStatus.APPLICATION and not first.is_possible_duplicate
    assert second.is_possible_duplicate  # совпал телефон
    notes = Notification.objects.filter(
        recipient=coordinator, kind=NotificationKind.NEW_VOLUNTEER_APPLICATION
    )
    assert notes.count() == 2


def test_92_2_coordinator_activates_volunteer_only_with_consent(
    make_user,
    verified_client,
    make_volunteer,
    refs,
):
    """Координатор проводит волонтёра от анкеты до «активен»; без согласия перевод
    в «активен» невозможен."""
    client = verified_client(make_user("coordinator"))
    volunteer = make_volunteer()
    status_url = reverse("volunteers:status", args=[volunteer.pk])
    for step in ("interview", "training"):
        assert client.post(status_url, {"to_status": step}).status_code == 302
    client.post(
        reverse("volunteers:training_add", args=[volunteer.pk]),
        {"training_type": refs["training"].pk, "completed_on": TODAY().isoformat()},
    )
    refused = client.post(status_url, {"to_status": "active"})
    assert refused.status_code == 200 and "согласи" in refused.content.decode()
    volunteer.refresh_from_db()
    assert volunteer.status == VolunteerStatus.TRAINING

    client.post(
        reverse("volunteers:consent_add", args=[volunteer.pk]),
        {"kind": "general", "signed_on": TODAY().isoformat(), "signer": "subject"},
    )
    assert client.post(status_url, {"to_status": "active"}).status_code == 302
    volunteer.refresh_from_db()
    assert volunteer.status == VolunteerStatus.ACTIVE


def test_92_3_curator_case_request_assistance_closed(verified_client, curator, brefs):
    """Куратор создаёт подопечного с согласием, заявку и факт помощи; заявка закрывается."""
    food = AssistanceType.objects.create(name="Продуктовый набор", unit="шт.")
    client = verified_client(curator)
    response = client.post(
        reverse("beneficiaries:create"),
        {
            "last_name": "Кузнецова",
            "first_name": "Галина",
            "birth_date": "1941-03-12",
            "address": "Москва, ул. Садовая, 3",
            "phone": "+79031112233",
            "category": brefs["category"].pk,
        },
    )
    assert response.status_code == 302, response.content.decode()[:300]
    b = Beneficiary.objects.get()
    assert b.curator == curator
    client.post(
        reverse("beneficiaries:consent_add", args=[b.pk]),
        {"kind": "general", "signed_on": TODAY().isoformat(), "signer": "subject"},
    )
    for step in ("assessment", "in_work"):
        client.post(reverse("beneficiaries:status", args=[b.pk]), {"to_status": step})
    b.refresh_from_db()
    assert b.status == CaseStatus.IN_WORK

    client.post(
        reverse("requests:create", args=[b.pk]),
        {"assistance_type": food.pk, "description": "Продукты на неделю", "urgency": "normal",
         "responsible": curator.pk},
    )  # fmt: skip
    r = HelpRequest.objects.get()
    client.post(reverse("requests:status", args=[r.pk]), {"to_status": "in_work"})
    client.post(
        reverse("requests:assistance_add", args=[b.pk]),
        {"request": r.pk, "assistance_type": food.pk, "provided_on": TODAY().isoformat(),
         "quantity": "1", "executor_user": curator.pk},
    )  # fmt: skip
    client.post(reverse("requests:status", args=[r.pk]), {"to_status": "done"})
    r.refresh_from_db()
    assert r.status == RequestStatus.DONE and r.assistance.count() == 1


def test_92_4_curator_never_sees_foreign_beneficiary(
    make_user,
    verified_client,
    curator,
    make_beneficiary,
):
    """Куратор не видит чужого подопечного ни в списке, ни по прямой ссылке, ни в поиске,
    ни в выгрузке."""
    foreign = make_beneficiary(last_name="Чужаков", curator=make_user("curator"))
    make_beneficiary(last_name="Своев")
    client = verified_client(curator)
    listing = client.get(reverse("beneficiaries:list"), {"q": "Чужаков"}, follow=True)
    assert foreign not in listing.context["page"].object_list
    assert client.get(foreign.get_absolute_url()).status_code == 404
    found = client.get(reverse("core:search"), {"q": "Чужаков"})
    assert all(foreign not in group.items for group in found.context["groups"])
    export = client.get(reverse("reports:export", args=["beneficiaries"]))
    rows = list(load_workbook(io.BytesIO(export.content)).active.iter_rows(values_only=True))
    assert "Чужаков" not in str(rows) and "Своев" in str(rows)


def test_92_5_coordinator_has_no_health_block(make_user, verified_client, make_beneficiary):
    """Координатор не видит блок данных о здоровье подопечного."""
    beneficiary = make_beneficiary()
    client = verified_client(make_user("coordinator"))
    card = client.get(beneficiary.get_absolute_url()).content.decode()
    assert "Защищённые данные" not in card
    assert client.get(reverse("beneficiaries:sensitive", args=[beneficiary.pk])).status_code in (
        403,
        404,
    )


def test_92_6_sensitive_view_and_export_are_audited(
    verified_client,
    curator,
    make_beneficiary,
):
    """Просмотр чувствительных данных и выгрузка фиксируются в журнале аудита."""
    beneficiary = make_beneficiary()
    client = verified_client(curator)
    client.get(reverse("beneficiaries:sensitive", args=[beneficiary.pk]))
    client.get(reverse("reports:export", args=["beneficiaries"]))
    actions = set(AuditLog.objects.filter(user=curator).values_list("action", flat=True))
    assert {AuditAction.VIEW_SENSITIVE, AuditAction.EXPORT} <= actions


def test_92_8_program_report_last_quarter_matches_manual(
    make_user,
    curator,
    make_beneficiary,
    brefs,
):
    """Отчёт по программе за прошлый квартал совпадает с ручным расчётом.

    Ручной расчёт: 2 подопечных, 3 факта, 2 500 ₽ за квартал; факт вне квартала
    и факт другой программы не входят.
    """
    quarter = reports.previous_quarter()
    food = AssistanceType.objects.create(name="Продукты", unit="шт.")
    money = AssistanceType.objects.create(name="Материальная помощь")
    program = brefs["program"]
    a, b = to_in_work(make_beneficiary()), to_in_work(make_beneficiary())
    inside = quarter.date_from + datetime.timedelta(days=10)
    rows = [
        (a, food, Decimal(2), None, program, inside),
        (a, money, None, Decimal(1000), program, quarter.date_to),
        (b, money, None, Decimal(1500), program, quarter.date_from),
        (b, money, None, Decimal(9999), program, quarter.date_to + datetime.timedelta(days=1)),
        (b, food, Decimal(5), None, None, inside),
    ]
    for person, kind, quantity, amount, prog, day in rows:
        request_services.register_assistance(
            person,
            assistance_data(
                kind, curator, quantity=quantity, amount=amount, program=prog, provided_on=day
            ),
        )
    report = reports.program_report(make_user("analyst"), quarter, program)
    assert (report.beneficiaries, report.facts, report.amount) == (2, 3, Decimal("2500.00"))


def test_92_11_no_entry_without_second_factor_and_lockout(client, make_user):
    """Вход без второго фактора невозможен; после 5 неверных паролей учётная запись
    блокируется."""
    user = make_user("curator")
    device = otp_device(user)
    login = reverse("accounts:login")
    client.post(login, {"username": user.username, "password": PASSWORD})
    assert client.get(reverse("core:home"))["Location"] == reverse("accounts:otp_verify")
    client.post(reverse("accounts:otp_verify"), {"token": current_token(device)})
    assert client.get(reverse("core:home")).status_code == 200

    other = make_user("curator")
    attacker = Client()
    for _ in range(5):
        attacker.post(login, {"username": other.username, "password": "неверный-пароль"})
    other.refresh_from_db()
    assert other.is_locked()
    blocked = attacker.post(login, {"username": other.username, "password": PASSWORD})
    assert "временно заблокирован" in blocked.content.decode()
