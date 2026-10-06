"""Разметка экранов: парные теги, без встроенных скриптов и стилей (CSP), подписи полей."""

import datetime
import re
from html.parser import HTMLParser

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from apps.volunteers import services

from .conftest import give_consent, years_ago

pytestmark = pytest.mark.django_db

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}


class TagBalance(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.ids: list[str] = []
        self.label_for: set[str] = set()
        self.inputs: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "style" in attrs:
            self.errors.append(f"style= в <{tag}>")
        if any(name.startswith("on") for name in attrs):
            self.errors.append(f"обработчик on* в <{tag}>")
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "label" and attrs.get("for"):
            self.label_for.add(attrs["for"])
        if tag in {"input", "select", "textarea"} and attrs.get("type") not in {"hidden", "submit"}:
            self.inputs.append(attrs.get("id") or "")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"лишний </{tag}>, открыт {self.stack[-1:]}")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()


def check(html: str) -> None:
    parser = TagBalance()
    parser.feed(html)
    assert not parser.errors, parser.errors
    assert parser.stack == [], parser.stack
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "встроенный <script>"
    duplicates = {i for i in parser.ids if parser.ids.count(i) > 1}
    assert not duplicates, f"повторяющиеся id: {duplicates}"
    unlabeled = [i for i in parser.inputs if i and i not in parser.label_for]
    assert not unlabeled, f"поля без <label for>: {unlabeled}"


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
