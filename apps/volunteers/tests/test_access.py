"""Права на карточку волонтёра по матрице 2.2 ТЗ (НФТ-ИБ-08: проверка на уровне объекта)."""

import pytest
from django.urls import reverse

from apps.volunteers.models import Volunteer

pytestmark = pytest.mark.django_db

LIST = reverse("volunteers:list")


def _urls(v):
    return {
        "detail": reverse("volunteers:detail", args=[v.pk]),
        "edit": reverse("volunteers:edit", args=[v.pk]),
        "status": reverse("volunteers:status", args=[v.pk]),
        "training": reverse("volunteers:training_add", args=[v.pk]),
        "hours": reverse("volunteers:hours_add", args=[v.pk]),
        "consent": reverse("volunteers:consent_add", args=[v.pk]),
    }


@pytest.mark.parametrize("role", ["admin", "coordinator"])
def test_full_access_roles(role, make_user, verified_client, make_volunteer):
    v = make_volunteer(email="anna@example.org")
    client = verified_client(make_user(role))
    assert client.get(LIST).status_code == 200
    for name, url in _urls(v).items():
        assert client.get(url).status_code == 200, name
    page = client.get(_urls(v)["detail"]).content.decode()
    assert "anna@example.org" in page and "14.03.1995" in page
    assert reverse("volunteers:create") in client.get(LIST).content.decode()


def test_curator_reads_limited_fields_only(make_user, verified_client, make_volunteer, refs):
    v = make_volunteer(
        email="anna@example.org",
        telegram_username="anna_help",
        district=refs["district"],
        skills=[refs["skill_med"]],
    )
    client = verified_client(make_user("curator"))
    page = client.get(_urls(v)["detail"]).content.decode()
    # Поля набора «О»: ФИО, телефон, Telegram, район, навыки, статус
    for visible in (v.full_name, "+7 916", "@anna_help", "Центральный", "Первая помощь", "Анкета"):
        assert visible in page, visible
    # Остальное не показывается
    for hidden in ("anna@example.org", "14.03.1995", "Согласия", "История статусов", "Часы"):
        assert hidden not in page, hidden
    # Кнопок изменения нет, а прямые ссылки на изменение закрыты
    assert _urls(v)["edit"] not in page
    for name in ("edit", "status", "training", "hours", "consent"):
        assert client.get(_urls(v)[name]).status_code == 403, name
    assert client.post(reverse("volunteers:archive", args=[v.pk])).status_code == 403
    assert client.get(reverse("volunteers:create")).status_code == 403


def test_curator_queryset_does_not_load_hidden_fields(make_user, make_volunteer):
    make_volunteer()
    curator = make_user("curator")
    v = Volunteer.objects.visible_to(curator).get()
    deferred = v.get_deferred_fields()
    assert {"birth_date", "email", "photo_id", "guardian_name", "guardian_phone"} <= deferred
    assert "phone" not in deferred


def test_curator_does_not_see_archived(make_user, verified_client, make_volunteer):
    from apps.volunteers.services import archive_volunteer

    v = make_volunteer()
    archive_volunteer(v)
    client = verified_client(make_user("curator"))
    assert client.get(_urls(v)["detail"]).status_code == 404
    assert client.get(LIST + "?archived=1").context["page"].paginator.count == 0


def test_curator_cannot_use_full_filters(make_user, verified_client, make_volunteer):
    make_volunteer(email="secret@example.org")
    client = verified_client(make_user("curator"))
    # Поиск по email (поле вне набора «О») куратору не работает
    response = client.get(LIST, {"q": "secret@example.org"})
    assert response.context["page"].paginator.count == 0
    assert "program" not in response.context["filter"].filters


def test_analyst_has_no_access(make_user, verified_client, make_volunteer):
    v = make_volunteer()
    client = verified_client(make_user("analyst"))
    assert client.get(LIST).status_code == 403
    assert client.get(_urls(v)["detail"]).status_code == 403
    assert client.get(_urls(v)["edit"]).status_code == 403
    assert not Volunteer.objects.visible_to(make_user("analyst")).exists()


def test_volunteer_role_sees_only_own_card_and_no_web(make_user, make_volunteer):
    own = make_volunteer()
    other = make_volunteer()
    user = make_user("volunteer")
    Volunteer.objects.filter(pk=own.pk).update(user=user)
    assert list(Volunteer.objects.visible_to(user)) == [own]
    assert other not in Volunteer.objects.visible_to(user)


def test_anonymous_and_unverified_redirected(client, make_user, make_volunteer):
    v = make_volunteer()
    response = client.get(_urls(v)["detail"])
    assert response.status_code == 302 and reverse("accounts:login") in response["Location"]
    client.force_login(make_user("coordinator"))  # пароль без второго фактора
    response = client.get(_urls(v)["detail"])
    assert response["Location"] == reverse("accounts:otp_verify")


def test_unknown_volunteer_is_404(make_user, verified_client):
    client = verified_client(make_user("coordinator"))
    url = reverse("volunteers:detail", args=["00000000-0000-0000-0000-000000000000"])
    assert client.get(url).status_code == 404


def test_navigation_link_by_role(make_user, verified_client):
    home = reverse("core:home")
    assert LIST in verified_client(make_user("curator")).get(home).content.decode()
    assert LIST not in verified_client(make_user("analyst")).get(home).content.decode()
