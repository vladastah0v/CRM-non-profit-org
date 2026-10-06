"""Приём анкет с сайта: ФТ-В-03, ФТ-В-04, ФТ-У-02, п. 7.1 ТЗ."""

import json
import time

import pytest
from django.test import Client
from django.urls import reverse

from apps.audit.models import AuditAction, AuditLog
from apps.consents.models import ConsentChannel, ConsentSigner
from apps.core.signatures import sign
from apps.notifications.models import Notification
from apps.volunteers.models import Volunteer, VolunteerStatus

from .conftest import years_ago

pytestmark = pytest.mark.django_db

URL = reverse("api-v1:intake-volunteer")
SECRET = "test-intake-secret"


def payload(**fields):
    return {
        "last_name": "Волкова",
        "first_name": "Ирина",
        "birth_date": "1990-04-20",
        "phone": "+7 (926) 000-11-22",
        "email": "irina@example.org",
        "telegram": "@irina_v",
        "city": "Москва",
        "district": "центральный",
        "skills": ["вождение", "Неизвестный навык"],
        "availability": [{"weekday": 6, "time_from": "10:00", "time_to": "15:00"}],
        "consent": True,
    } | fields


def post(
    client, data, *, secret=SECRET, timestamp=None, signature=None, ip="203.0.113.5", **headers
):
    body = data if isinstance(data, bytes) else json.dumps(data).encode()
    ts = str(int(time.time()) if timestamp is None else timestamp)
    return client.post(
        URL,
        body,
        content_type=headers.pop("content_type", "application/json"),
        HTTP_X_TIMESTAMP=ts,
        HTTP_X_SIGNATURE=signature or sign(secret, ts, body),
        REMOTE_ADDR=ip,
        **headers,
    )


@pytest.fixture
def api():
    return Client(enforce_csrf_checks=True)  # внешний сайт не имеет CSRF-токена


def test_ft_v_03_creates_application_card(api, refs, make_user):
    coordinator = make_user("coordinator")
    response = post(api, payload())
    assert response.status_code == 201 and response.json() == {"status": "accepted"}

    v = Volunteer.objects.get()
    assert v.status == VolunteerStatus.APPLICATION
    assert v.phone == "+79260001122" and v.telegram_username == "irina_v"
    assert v.district == refs["district"]  # найден по названию без учёта регистра
    assert list(v.skills.all()) == [refs["skill_drive"]]  # неизвестный навык пропущен
    assert v.source is not None and v.source.name == "Сайт фонда"
    assert v.availability.get().weekday == 6
    assert not v.is_possible_duplicate

    # Факт и время согласия сохранены (п. 7.1)
    consent = v.consents.get()
    assert consent.channel == ConsentChannel.WEBSITE and consent.is_valid_on()
    assert consent.created_at is not None

    # ФТ-У-02: уведомление координатору, без ПДн в тексте
    note = Notification.objects.get(recipient=coordinator)
    assert note.url == v.get_absolute_url() and "Волкова" not in note.title + note.body
    log = AuditLog.objects.get(action=AuditAction.CREATE, object_id=str(v.pk))
    assert log.ip == "203.0.113.5" and log.extra["channel"] == "website"


def test_notification_falls_back_to_admins(api, make_user):
    admin = make_user("admin")
    post(api, payload())
    assert Notification.objects.filter(recipient=admin).exists()


def test_ft_v_04_duplicate_flagged_but_not_revealed(api, make_user):
    make_user("coordinator")
    assert post(api, payload()).status_code == 201
    response = post(api, payload(email="other@example.org"))  # тот же телефон
    assert response.status_code == 201 and response.json() == {"status": "accepted"}
    flags = sorted(Volunteer.objects.values_list("is_possible_duplicate", flat=True))
    assert flags == [False, True]
    latest = Notification.objects.order_by("-created_at").first()
    assert latest is not None and "дубль" in latest.body


@pytest.mark.parametrize(
    "headers",
    [
        {"signature": "sha256=" + "0" * 64},  # неверная подпись
        {"secret": "wrong-secret"},  # подписано другим ключом
        {"timestamp": int(time.time()) - 600},  # устарела
        {"timestamp": int(time.time()) + 600},  # из будущего
        {"signature": "md5=abc"},
    ],
)
def test_signature_required(api, headers):
    assert post(api, payload(), **headers).status_code == 403
    assert not Volunteer.objects.exists()


def test_missing_headers_rejected(api):
    response = api.post(URL, json.dumps(payload()), content_type="application/json")
    assert response.status_code == 403


def test_tampered_body_rejected(api):
    ts = str(int(time.time()))
    signature = sign(SECRET, ts, json.dumps(payload()).encode())
    response = post(api, payload(last_name="Подмена"), timestamp=ts, signature=signature)
    assert response.status_code == 403


def test_replay_rejected(api):
    ts = int(time.time())
    assert post(api, payload(), timestamp=ts).status_code == 201
    assert post(api, payload(), timestamp=ts).status_code == 409
    assert Volunteer.objects.count() == 1


def test_invalid_request_can_be_resent(api):
    """Ошибка в данных не «сжигает» подпись: сайт может исправить и повторить."""
    ts = int(time.time())
    data = payload(phone="123")
    response = post(api, data, timestamp=ts)
    assert response.status_code == 400 and "phone" in response.json()["errors"]
    assert post(api, data, timestamp=ts).status_code == 400  # не 409


def test_consent_is_mandatory(api):
    response = post(api, payload(consent=False))
    assert response.status_code == 400 and "consent" in response.json()["errors"]
    response = post(api, {k: v for k, v in payload().items() if k != "consent"})
    assert response.status_code == 400


def test_ft_v_08_minor_requires_guardian_and_guardian_consent(api):
    minor = {"birth_date": years_ago(15).isoformat()}
    response = post(api, payload(**minor))
    errors = response.json()["errors"]
    assert response.status_code == 400
    assert {"guardian_name", "guardian_phone", "guardian_consent"} <= set(errors)

    response = post(
        api,
        payload(
            **minor,
            guardian_name="Волкова Анна",
            guardian_phone="+79260001199",
            guardian_relation="мать",
            guardian_consent=True,
        ),
    )
    assert response.status_code == 201
    consent = Volunteer.objects.get().consents.get()
    assert consent.signer == ConsentSigner.REPRESENTATIVE
    assert consent.signer_name == "Волкова Анна"


def test_bad_json_and_content_type(api):
    assert post(api, b"{not json").status_code == 400
    assert post(api, b"[]").status_code == 400
    assert post(api, payload(), content_type="text/plain").status_code == 415


def test_body_size_limit(api):
    assert post(api, payload(city="x" * 70_000)).status_code == 413


def test_rate_limit_10_per_minute_per_ip(api):
    for _ in range(10):
        assert post(api, payload(consent=False)).status_code == 400
    assert post(api, payload(consent=False)).status_code == 429
    # Другой IP не ограничен
    assert post(api, payload(consent=False), ip="203.0.113.77").status_code == 400


def test_rate_limit_ignores_spoofed_forwarded_for(api):
    for i in range(10):
        post(api, payload(consent=False), HTTP_X_FORWARDED_FOR=f"10.0.0.{i}")
    response = post(api, payload(consent=False), HTTP_X_FORWARDED_FOR="10.0.0.99")
    assert response.status_code == 429


def test_disabled_without_secret(api, settings):
    settings.INTAKE_HMAC_SECRET = ""
    assert post(api, payload()).status_code == 503


def test_get_not_allowed(api):
    assert api.get(URL).status_code == 405
