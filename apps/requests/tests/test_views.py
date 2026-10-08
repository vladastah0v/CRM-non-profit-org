"""Экраны заявок и помощи; права по матрице 2.2 (ФТ-З-04: «Мои заявки»)."""

import datetime

import pytest
from django.urls import reverse

from apps.beneficiaries import services as beneficiary_services
from apps.core.tests.markup import check_markup
from apps.notifications.models import Notification, NotificationKind
from apps.requests import services
from apps.requests.models import Assistance, HelpRequest, RequestStatus

from .conftest import TODAY, assistance_data

pytestmark = pytest.mark.django_db
LIST = reverse("requests:list")


def names(response):
    return [r.description for r in response.context["page"].object_list]


@pytest.fixture
def curator_client(verified_client, curator):
    return verified_client(curator)


# --- ФТ-З-04: «Мои заявки» ---------------------------------------------------------------------


def test_curator_starts_with_my_open_requests(curator_client):
    response = curator_client.get(LIST)
    assert response.status_code == 302
    assert response["Location"] == f"{LIST}?scope=mine&status=open"


def test_my_requests_sorted_by_due_overdue_highlighted(curator_client, make_request):
    make_request(description="Позже", due_date=TODAY() + datetime.timedelta(days=10))
    make_request(description="Просрочена", due_date=TODAY() - datetime.timedelta(days=1))
    make_request(description="Без срока", due_date=None)
    make_request(description="Скоро", due_date=TODAY() + datetime.timedelta(days=1))
    response = curator_client.get(LIST, {"scope": "mine", "status": "open"})
    assert names(response) == ["Просрочена", "Скоро", "Позже", "Без срока"]
    page = response.content.decode()
    assert page.count("просрочена</span>") == 1 and "border-red-300 bg-red-50" in page
    overdue = curator_client.get(LIST, {"overdue": "on"})
    assert names(overdue) == ["Просрочена"]


def test_scope_mine_includes_requests_where_responsible(
    make_user, verified_client, make_request, make_beneficiary
):
    other_curator = make_user("curator")
    second = make_user("curator")
    make_request(
        make_beneficiary(curator=other_curator), description="Мне поручено", responsible=second
    )
    make_request(make_beneficiary(curator=other_curator), description="Чужая")
    client = verified_client(second)
    assert names(client.get(LIST, {"scope": "mine"})) == ["Мне поручено"]


# --- Права (матрица 2.2) -----------------------------------------------------------------------


def test_curator_does_not_see_foreign_requests(
    make_user, verified_client, make_request, make_beneficiary, curator_client
):
    foreign = make_request(make_beneficiary(curator=make_user("curator")), description="Чужая")
    own = make_request(description="Своя")
    assert names(curator_client.get(LIST, {"status": ""})) == ["Своя"]
    assert curator_client.get(foreign.get_absolute_url()).status_code == 404
    assert curator_client.get(reverse("requests:edit", args=[foreign.pk])).status_code == 404
    assert curator_client.get(own.get_absolute_url()).status_code == 200
    url = reverse("requests:create", args=[foreign.beneficiary_id])
    assert curator_client.get(url).status_code == 404


def test_coordinator_reads_only(make_user, verified_client, make_request, food, curator):
    r = make_request()
    services.register_assistance(r.beneficiary, assistance_data(food, curator), help_request=r)
    client = verified_client(make_user("coordinator"))
    assert client.get(LIST).status_code == 200
    page = client.get(r.get_absolute_url()).content.decode()
    assert r.description in page and "Сменить статус" not in page
    for name in ("edit", "status"):
        assert client.get(reverse(f"requests:{name}", args=[r.pk])).status_code == 403
    assert (
        client.get(reverse("requests:assistance_add", args=[r.beneficiary_id])).status_code == 403
    )
    # В карточке подопечного (набор «О») — заявки и помощь только для чтения
    card = client.get(r.beneficiary.get_absolute_url()).content.decode()
    assert "Продуктовый набор" in card and "+ Новая заявка" not in card


def test_analyst_has_no_access(make_user, verified_client, make_request):
    r = make_request()
    client = verified_client(make_user("analyst"))
    assert client.get(LIST).status_code == 403
    assert client.get(r.get_absolute_url()).status_code == 403
    assert not Assistance.objects.visible_to(make_user("analyst")).exists()


def test_only_admin_archives_request(make_user, verified_client, make_request, curator_client):
    r = make_request()
    url = reverse("requests:archive", args=[r.pk])
    assert curator_client.post(url).status_code == 403
    verified_client(make_user("admin")).post(url)
    r.refresh_from_db()
    assert r.is_archived


# --- Экраны ------------------------------------------------------------------------------------


def test_create_request_from_card(curator_client, make_beneficiary, food, curator):
    b = make_beneficiary()
    response = curator_client.post(
        reverse("requests:create", args=[b.pk]),
        {
            "assistance_type": str(food.pk),
            "description": "Лекарства",
            "urgency": "urgent",
            "due_date": TODAY().isoformat(),
        },
    )
    r = HelpRequest.objects.get()
    assert response["Location"] == r.get_absolute_url()
    assert r.responsible == curator and r.urgency == "urgent"
    card = curator_client.get(b.get_absolute_url()).content.decode()
    assert r.get_absolute_url() in card and "срочная" in card


def test_full_request_flow(curator_client, make_request, curator):
    r = make_request()
    status_url = reverse("requests:status", args=[r.pk])
    curator_client.post(status_url, {"to_status": "in_work"})
    page = curator_client.get(status_url).content.decode()
    assert "сначала зарегистрируйте оказанную помощь" in page
    add_url = reverse("requests:assistance_add", args=[r.beneficiary_id]) + f"?request={r.pk}"
    form_page = curator_client.get(add_url).content.decode()
    assert f'value="{r.pk}" selected' in form_page  # заявка подставлена
    response = curator_client.post(
        add_url,
        {
            "request": str(r.pk),
            "assistance_type": str(r.assistance_type_id),
            "provided_on": TODAY().isoformat(),
            "quantity": "3",
            "executor_user": str(curator.pk),
        },
    )
    assert response["Location"] == r.get_absolute_url()
    curator_client.post(status_url, {"to_status": "done"})
    r.refresh_from_db()
    assert r.status == RequestStatus.DONE
    detail = curator_client.get(r.get_absolute_url()).content.decode()
    assert "3 шт." in detail and "Выполнена" in detail
    # ФТ-П-05: заявка и помощь — в ленте истории подопечного
    card = curator_client.get(r.beneficiary.get_absolute_url()).content.decode()
    for event in ("Создана заявка", "Заявка: Новая → В работе", "Оказана помощь"):
        assert event in card, event


def test_assistance_form_errors(curator_client, make_beneficiary, food):
    b = make_beneficiary()
    response = curator_client.post(
        reverse("requests:assistance_add", args=[b.pk]),
        {"assistance_type": str(food.pk), "provided_on": TODAY().isoformat()},
    )
    errors = response.context["form"].errors
    assert "quantity" in errors and "executor_user" in errors


def test_invalid_request_param_ignored(curator_client, make_beneficiary):
    url = reverse("requests:assistance_add", args=[make_beneficiary().pk]) + "?request=xyz"
    assert curator_client.get(url).status_code == 200


def test_edit_and_archive_assistance(curator_client, make_beneficiary, food, curator):
    b = make_beneficiary()
    a = services.register_assistance(b, assistance_data(food, curator))
    response = curator_client.post(
        reverse("requests:assistance_edit", args=[a.pk]),
        {
            "assistance_type": str(food.pk),
            "provided_on": TODAY().isoformat(),
            "amount": "250",
            "executor_user": str(curator.pk),
        },
    )
    assert response.status_code == 302
    a.refresh_from_db()
    assert a.amount == 250
    curator_client.post(reverse("requests:assistance_archive", args=[a.pk]))
    a.refresh_from_db()
    assert a.is_archived


def test_search_by_beneficiary_and_edit_responsible(
    make_user, verified_client, make_request, make_beneficiary
):
    target = make_request(make_beneficiary(last_name="Иголкина"), description="Найти")
    make_request(make_beneficiary(last_name="Другова"), description="Не найти")
    admin = verified_client(make_user("admin"))
    assert names(admin.get(LIST, {"q": "иголкина", "status": ""})) == ["Найти"]

    new_responsible = make_user("curator")
    admin.post(
        reverse("requests:edit", args=[target.pk]),
        {
            "assistance_type": str(target.assistance_type_id),
            "description": "Найти и купить",
            "urgency": "normal",
            "responsible": str(new_responsible.pk),
        },
    )
    target.refresh_from_db()
    assert target.responsible == new_responsible and target.description == "Найти и купить"
    assert Notification.objects.filter(
        recipient=new_responsible, kind=NotificationKind.REQUEST_ASSIGNED
    ).exists()


def test_closed_case_has_no_new_request_button(curator_client, make_beneficiary, brefs):
    b = make_beneficiary()
    beneficiary_services.change_status(b, "closed", reason=brefs["reason"], outcome="Итог")
    assert "+ Новая заявка" not in curator_client.get(b.get_absolute_url()).content.decode()


def test_pages_markup(make_user, verified_client, make_request, food, curator):
    r = make_request()
    a = services.register_assistance(r.beneficiary, assistance_data(food, curator), help_request=r)
    admin = verified_client(make_user("admin"))
    pages = [
        f"{LIST}?status=",
        r.get_absolute_url(),
        reverse("requests:create", args=[r.beneficiary_id]),
        reverse("requests:edit", args=[r.pk]),
        reverse("requests:status", args=[r.pk]),
        reverse("requests:assistance_add", args=[r.beneficiary_id]),
        reverse("requests:assistance_edit", args=[a.pk]),
        r.beneficiary.get_absolute_url(),
    ]
    for page in pages:
        response = admin.get(page)
        assert response.status_code == 200, page
        check_markup(response.content.decode())
