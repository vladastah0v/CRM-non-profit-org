"""п. 3.10: глобальный поиск по ФИО и телефону; п. 9.1: чужой подопечный не находится."""

import pytest
from django.urls import reverse

from apps.core.tests.markup import check_markup

pytestmark = pytest.mark.django_db
URL = reverse("core:search")


def found(response, title):
    group = next(g for g in response.context["groups"] if g.title == title)
    return [item.pk for item in group.items]


def test_search_by_name_and_phone(verified_client, make_user, make_active, make_beneficiary):
    volunteer = make_active(last_name="Соколова")
    beneficiary = make_beneficiary(last_name="Соколов", phone="+79035550101")
    client = verified_client(make_user("admin"))
    response = client.get(URL, {"q": "соколов"})
    check_markup(response.content.decode())
    assert found(response, "Волонтёры") == [volunteer.pk]
    assert found(response, "Подопечные") == [beneficiary.pk]
    by_phone = client.get(URL, {"q": "8 903 555-01-01"})
    assert found(by_phone, "Подопечные") == [beneficiary.pk]


def test_curator_does_not_find_foreign_beneficiary(
    verified_client, make_user, curator, make_beneficiary
):
    mine = make_beneficiary(last_name="Орлов")
    make_beneficiary(last_name="Орлов", curator=make_user("curator"))
    response = verified_client(curator).get(URL, {"q": "Орлов"})
    assert found(response, "Подопечные") == [mine.pk]


def test_coordinator_finds_beneficiaries_limited(verified_client, coordinator, make_beneficiary):
    make_beneficiary(last_name="Лебедев")
    response = verified_client(coordinator).get(URL, {"q": "Лебедев"})
    item = response.context["groups"][1].items[0]
    assert "birth_date" in item.get_deferred_fields()  # набор «О»: дата рождения не загружена


def test_short_query_and_analyst(verified_client, make_user):
    client = verified_client(make_user("admin"))
    response = client.get(URL, {"q": "а"})
    assert response.context["too_short"] and response.context["groups"] == []
    analyst = verified_client(make_user("analyst")).get(URL, {"q": "Иванов"})
    assert analyst.status_code == 200 and not analyst.context["can_search"]
