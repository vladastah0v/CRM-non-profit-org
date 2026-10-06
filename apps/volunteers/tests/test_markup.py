"""Разметка экранов: парные теги, без встроенных скриптов и стилей (CSP), подписи полей."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.tests.markup import check_markup as check
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from apps.volunteers import services

from .conftest import give_consent, years_ago

pytestmark = pytest.mark.django_db


def test_pages_markup(make_user, verified_client, make_volunteer, refs):
    coordinator = make_user("coordinator")
    v = make_volunteer(
        birth_date=years_ago(16),
        guardian_name="Мать",
        guardian_phone="+79160000099",
        skills=[refs["skill_drive"]],
        availability=[(0, datetime.time(9), datetime.time(12))],
    )
    consent = give_consent(v)
    services.add_training(v, training_type=refs["training"], completed_on=timezone.localdate())
    notify([coordinator], NotificationKind.NEW_VOLUNTEER_APPLICATION, "Анкета", url="/")

    pages = [
        reverse("volunteers:list"),
        reverse("volunteers:create"),
        reverse("volunteers:detail", args=[v.pk]),
        reverse("volunteers:edit", args=[v.pk]),
        reverse("volunteers:status", args=[v.pk]),
        reverse("volunteers:training_add", args=[v.pk]),
        reverse("volunteers:hours_add", args=[v.pk]),
        reverse("volunteers:consent_add", args=[v.pk]),
        reverse("volunteers:consent_revoke", args=[v.pk, consent.pk]),
        reverse("notifications:list"),
        reverse("core:home"),
    ]
    client = verified_client(coordinator)
    for url in pages:
        response = client.get(url)
        assert response.status_code == 200, url
        check(response.content.decode())

    curator = verified_client(make_user("curator"))
    for url in (reverse("volunteers:list"), reverse("volunteers:detail", args=[v.pk])):
        check(curator.get(url).content.decode())
