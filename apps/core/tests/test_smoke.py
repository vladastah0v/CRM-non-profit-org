import datetime

import pytest
from django.urls import reverse
from django.utils import formats

pytestmark = pytest.mark.django_db


def test_healthz_ok(client):
    response = client.get(reverse("core:healthz"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": True, "cache": True}


def test_home_requires_login(client):
    response = client.get(reverse("core:home"))
    assert response.status_code == 302
    assert reverse("accounts:login") in response["Location"]


def test_home_for_verified_user(make_user, verified_client):
    response = verified_client(make_user("coordinator")).get(reverse("core:home"))
    assert response.status_code == 200
    assert "CRM НКО" in response.content.decode()


def test_login_page_in_russian(client):
    response = client.get(reverse("accounts:login"))
    assert response.status_code == 200
    assert "Вход в CRM НКО" in response.content.decode()


def test_date_format_dd_mm_yyyy():
    date = datetime.date(2026, 10, 5)
    assert formats.date_format(date, "SHORT_DATE_FORMAT") == "05.10.2026"
    assert formats.date_format(date) == "05.10.2026"


def test_api_docs_closed_for_anonymous(client):
    response = client.get(reverse("api-v1:schema"))
    assert response.status_code == 403
