"""Приём обращений подопечных с сайта: п. 7.1 ТЗ, ФТ-П-06 (дубли)."""

import json
import time

import pytest
from django.test import Client
from django.urls import reverse

from apps.beneficiaries import services
from apps.beneficiaries.models import Beneficiary, CaseStatus
from apps.consents.models import ConsentChannel, ConsentSigner
from apps.core.signatures import sign
from apps.interactions.models import Interaction
from apps.notifications.models import Notification, NotificationKind

from .conftest import years_ago

pytestmark = pytest.mark.django_db

URL = reverse("api-v1:intake-beneficiary")
SECRET = "test-intake-secret"


def payload(**fields):
    return {
        "last_name": "Кузнецова",
        "first_name": "Галина",
        "middle_name": "Петровна",
        "birth_date": "12.03.1941",
        "phone": "8 (903) 111-22-33",
        "address": "Москва, ул. Садовая, 3",
        "category": "пожилые одинокие",
        "need": "Нужна помощь с продуктами раз в неделю",
        "consent": True,
    } | fields


def post(data, *, signature=None):
    body = json.dumps(data).encode()
    ts = str(int(time.time()))
    return Client(enforce_csrf_checks=True).post(
        URL,
        body,
        content_type="application/json",
        HTTP_X_TIMESTAMP=ts,
        HTTP_X_SIGNATURE=signature or sign(SECRET, ts, body),
        REMOTE_ADDR="203.0.113.7",
    )


def test_p71_creates_new_case_with_consent_and_need(brefs, make_user):
    admin = make_user("admin")
    response = post(payload())
    assert response.status_code == 201 and response.json() == {"status": "accepted"}
    b = Beneficiary.objects.get()
    assert (b.status, b.curator, b.phone) == (CaseStatus.NEW, None, "+79031112233")
    assert (
        b.category == brefs["category"] and b.source is not None and b.source.name == "Сайт фонда"
    )
    assert not b.is_possible_duplicate
    consent = b.consents.get()
    assert consent.channel == ConsentChannel.WEBSITE and consent.signer == ConsentSigner.SUBJECT
    assert Interaction.objects.get(beneficiary=b).text.endswith("продуктами раз в неделю")
    note = Notification.objects.get(recipient=admin)
    assert note.kind == NotificationKind.NEW_BENEFICIARY_APPLICATION
    assert "Кузнецова" not in note.title + note.body  # без ФИО (п. 3.8)


def test_p71_rejects_unsigned_and_without_consent(db):
    assert post(payload(), signature="sha256=00").status_code == 403
    response = post(payload(consent=False))
    assert response.status_code == 400 and "consent" in response.json()["errors"]
    assert not Beneficiary.objects.exists()


def test_p71_minor_requires_representative(db):
    minor = payload(birth_date=years_ago(10).isoformat())
    response = post(minor)
    assert set(response.json()["errors"]) >= {"representative_name", "representative_consent"}
    ok = post(
        minor
        | {
            "representative_name": "Кузнецова Ольга",
            "representative_relation": "мать",
            "representative_phone": "+79035550000",
            "representative_consent": True,
        }
    )
    assert ok.status_code == 201
    b = Beneficiary.objects.get()
    assert b.related_persons.get().is_legal_representative
    assert b.consents.get().signer == ConsentSigner.REPRESENTATIVE


def test_fp06_duplicate_is_flagged_and_resolved(make_beneficiary, make_user, verified_client):
    existing = make_beneficiary(phone="+79031112233")
    assert post(payload()).status_code == 201
    new = Beneficiary.objects.exclude(pk=existing.pk).get()
    assert new.is_possible_duplicate
    admin = make_user("admin")
    client = verified_client(admin)
    page = client.get(new.get_absolute_url()).content.decode()
    assert "Возможный дубль" in page and existing.full_name in page
    client.post(
        reverse("beneficiaries:duplicate_resolve", args=[new.pk]), {"decision": "different"}
    )
    new.refresh_from_db()
    assert not new.is_possible_duplicate
    # Куратор не принимает решение по дублю
    curator_client = verified_client(new.curator or make_user("curator"))
    response = curator_client.post(
        reverse("beneficiaries:duplicate_resolve", args=[new.pk]), {"decision": "same"}
    )
    assert response.status_code == 403


def test_resolve_same_person_archives(make_beneficiary):
    existing = make_beneficiary(phone="+79031112233")
    post(payload())
    new = Beneficiary.objects.exclude(pk=existing.pk).get()
    services.resolve_duplicate(new, same_person=True)
    new.refresh_from_db()
    assert new.is_archived
