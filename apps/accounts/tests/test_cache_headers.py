"""Страницы сотрудников не остаются в кэше браузера (замечание проверки ZAP, этап 9)."""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from apps.accounts.middleware import NoStoreForSignedInMiddleware

pytestmark = pytest.mark.django_db


def test_signed_in_page_is_not_stored(make_user, verified_client):
    client = verified_client(make_user("curator"))
    response = client.get(reverse("core:home"))
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert "private" in response["Cache-Control"]


def test_htmx_partial_is_not_stored(make_user, verified_client):
    client = verified_client(make_user("curator"))
    response = client.get(reverse("beneficiaries:list"), HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]


def test_htmx_keeps_no_page_snapshots(make_user, verified_client):
    """htmx не сохраняет снимки страниц (списки с ФИО) в localStorage браузера."""
    client = verified_client(make_user("curator"))
    content = client.get(reverse("core:home")).content.decode()
    assert '"historyCacheSize": 0' in content
    assert '"refreshOnHistoryMiss": true' in content


def test_header_set_by_view_is_kept(make_user):
    request = RequestFactory().get("/")
    request.user = make_user("curator")

    def view(request):
        response = HttpResponse()
        response["Cache-Control"] = "no-store, private"
        return response

    response = NoStoreForSignedInMiddleware(view)(request)
    assert response["Cache-Control"] == "no-store, private"


def test_anonymous_response_unchanged():
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    response = NoStoreForSignedInMiddleware(lambda request: HttpResponse())(request)
    assert not response.has_header("Cache-Control")
