"""Права на карточку подопечного: матрица 2.2, правила 2.3, приёмочные сценарии 9.2 ТЗ."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.audit.models import AuditAction, AuditLog
from apps.beneficiaries import services
from apps.beneficiaries.models import Beneficiary
from apps.consents.files import make_download_url
from apps.consents.models import ConsentKind

from .conftest import give_consent

pytestmark = pytest.mark.django_db

LIST = reverse("beneficiaries:list")
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

MANAGE_URLS = (
    "edit",
    "status",
    "sensitive",
    "sensitive_edit",
    "document_add",
    "related_add",
    "consent_add",
)


def url(name, b, *args):
    return reverse(f"beneficiaries:{name}", args=[b.pk, *args])


def _names(response):
    return [b.last_name for b in response.context["page"].object_list]


# --- 9.2: «Куратор не видит чужого подопечного ни в списке, ни по прямой ссылке, ни в поиске»


def test_curator_sees_only_own(make_user, verified_client, make_beneficiary, curator):
    own = make_beneficiary(last_name="Свой")
    other_curator = make_user("curator")
    foreign = make_beneficiary(last_name="Чужой", curator=other_curator)
    give_consent(foreign, scan=SimpleUploadedFile("с.pdf", PDF))
    client = verified_client(curator)

    assert _names(client.get(LIST)) == ["Свой"]  # список
    assert _names(client.get(LIST, {"q": "Чужой"})) == []  # поиск
    assert _names(client.get(LIST, {"q": foreign.phone})) == []
    assert client.get(url("detail", foreign)).status_code == 404  # прямая ссылка
    for name in MANAGE_URLS:
        assert client.get(url(name, foreign)).status_code == 404, name
    # Скан согласия чужого подопечного — тоже нет
    scan = foreign.consents.get().scan
    assert client.get(make_download_url(scan, curator)).status_code == 404
    assert client.get(url("detail", own)).status_code == 200


# --- 9.2: «Координатор не видит блок данных о здоровье подопечного»


def test_coordinator_limited_and_no_sensitive(make_user, verified_client, make_beneficiary, brefs):
    b = make_beneficiary(category=brefs["category"])
    give_consent(b, kind=ConsentKind.SPECIAL)
    services.save_sensitive(b, {"health": "Диабет", "passport": "4510 123456"})
    client = verified_client(make_user("coordinator"))

    page = client.get(url("detail", b)).content.decode()
    for visible in (b.full_name, "Москва, ул. Ленина", "Пожилые одинокие", "Кураторова"):
        assert visible in page, visible
    for hidden in ("09.05.1948", "Диабет", "4510", "Защищённые данные", "Согласия", "История"):
        assert hidden not in page, hidden
    for name in MANAGE_URLS:
        assert client.get(url(name, b)).status_code == 403, name
    assert client.get(reverse("beneficiaries:create")).status_code == 403
    deferred = Beneficiary.objects.visible_to(make_user("coordinator")).get().get_deferred_fields()
    assert {"birth_date", "source_id", "outcome"} <= deferred


# --- 9.2: «Просмотр чувствительных данных и выгрузка фиксируются в журнале аудита»


def test_sensitive_view_is_audited(verified_client, make_beneficiary, curator):
    b = make_beneficiary()
    client = verified_client(curator)
    client.get(url("detail", b))  # обычная карточка — без отметки
    assert not AuditLog.objects.filter(action=AuditAction.VIEW_SENSITIVE).exists()
    client.get(url("sensitive", b))
    client.get(url("sensitive_edit", b))
    logs = AuditLog.objects.filter(action=AuditAction.VIEW_SENSITIVE, object_id=str(b.pk))
    assert logs.count() == 2
    assert {log.user for log in logs} == {curator}


def test_sensitive_page_not_cached(verified_client, make_beneficiary, curator):
    response = verified_client(curator).get(url("sensitive", make_beneficiary()))
    assert "no-store" in response["Cache-Control"] or "no-cache" in response["Cache-Control"]


# --- Остальные роли ----------------------------------------------------------------------------


def test_analyst_has_no_access(make_user, verified_client, make_beneficiary):
    b = make_beneficiary()
    client = verified_client(make_user("analyst"))
    assert client.get(LIST).status_code == 403
    assert client.get(url("detail", b)).status_code == 403
    assert not Beneficiary.objects.visible_to(make_user("analyst")).exists()


def test_admin_full_access(make_user, verified_client, make_beneficiary):
    b = make_beneficiary()
    client = verified_client(make_user("admin"))
    for name in (*MANAGE_URLS, "curators"):
        assert client.get(url(name, b)).status_code == 200, name


def test_only_admin_manages_curators(verified_client, make_beneficiary, curator):
    b = make_beneficiary()
    client = verified_client(curator)  # даже ответственный куратор
    assert client.get(url("curators", b)).status_code == 403
    assert "Кураторы" not in client.get(url("detail", b)).content.decode()


def test_shared_curator_has_full_access(make_user, verified_client, make_beneficiary):
    b = make_beneficiary()
    second = make_user("curator")
    services.grant_access(b, second)
    client = verified_client(second)
    assert client.get(url("sensitive", b)).status_code == 200
    assert "Защищённые данные" in client.get(url("detail", b)).content.decode()


def test_curator_coordinator_combination(make_user, verified_client, make_beneficiary, curator):
    """Права суммируются: свои — полностью, чужие — набор «О» координатора."""
    from apps.accounts.models import Role, UserRole

    own = make_beneficiary(last_name="Свой")
    foreign = make_beneficiary(last_name="Чужой", curator=make_user("curator"))
    UserRole.objects.create(user=curator, role=Role.objects.get(code="coordinator"))
    client = verified_client(curator)
    assert sorted(_names(client.get(LIST))) == ["Свой", "Чужой"]
    assert "Защищённые данные" in client.get(url("detail", own)).content.decode()
    foreign_page = client.get(url("detail", foreign)).content.decode()
    assert "Защищённые данные" not in foreign_page and "09.05.1948" not in foreign_page
    assert client.get(url("sensitive", foreign)).status_code == 403


def test_duplicate_warning_does_not_reveal_foreign_card(
    make_user, verified_client, make_beneficiary, curator
):
    foreign = make_beneficiary(last_name="Тайнова", curator=make_user("curator"))
    client = verified_client(curator)
    data = {
        "last_name": "Тайнова",
        "first_name": foreign.first_name,
        "middle_name": foreign.middle_name,
        "birth_date": "1948-05-09",
        "phone": "",
        "address": "",
    }
    page = client.post(reverse("beneficiaries:create"), data).content.decode()
    assert "Карточка, к которой у вас нет доступа" in page
    assert url("detail", foreign) not in page
