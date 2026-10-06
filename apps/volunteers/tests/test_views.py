"""Экраны модуля «Волонтёры»: создание, карточка, действия, список с фильтрами (ФТ-В-05)."""

import datetime

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditAction, AuditLog
from apps.core.models import SavedListFilter
from apps.volunteers import services
from apps.volunteers.models import Volunteer, VolunteerStatus

from .conftest import give_consent

pytestmark = pytest.mark.django_db

LIST = reverse("volunteers:list")
CREATE = reverse("volunteers:create")
TODAY = timezone.localdate
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def form_data(**fields):
    data = {
        "last_name": "Кузнецова",
        "first_name": "Ольга",
        "middle_name": "",
        "birth_date": "1992-07-01",
        "phone": "+7 903 111-22-33",
        "email": "olga@example.org",
        "telegram_username": "",
        "city": "Москва",
        "slots-TOTAL_FORMS": "2",
        "slots-INITIAL_FORMS": "0",
        "slots-MIN_NUM_FORMS": "0",
        "slots-MAX_NUM_FORMS": "21",
        "slots-0-weekday": "5",
        "slots-0-time_from": "10:00",
        "slots-0-time_to": "14:00",
    }
    data.update(fields)
    return data


@pytest.fixture
def coordinator_client(make_user, verified_client):
    return verified_client(make_user("coordinator"))


# --- Создание и изменение ---------------------------------------------------------------------


def test_create_volunteer(coordinator_client, refs):
    data = form_data(skills=[str(refs["skill_drive"].pk)], district=str(refs["district"].pk))
    response = coordinator_client.post(CREATE, data)
    v = Volunteer.objects.get()
    assert response.status_code == 302 and response["Location"] == v.get_absolute_url()
    assert v.phone == "+79031112233" and v.district == refs["district"]
    assert v.availability.get().weekday == 5
    page = coordinator_client.get(response["Location"]).content.decode()
    assert "Карточка волонтёра создана" in page and "Кузнецова Ольга" in page


def test_create_shows_errors(coordinator_client):
    response = coordinator_client.post(CREATE, form_data(phone="123", last_name=""))
    assert response.status_code == 200
    errors = response.context["form"].errors
    assert "phone" in errors and "last_name" in errors
    assert not Volunteer.objects.exists()


def test_create_minor_requires_guardian(coordinator_client):
    birth = TODAY().replace(year=TODAY().year - 15) - datetime.timedelta(days=10)
    response = coordinator_client.post(CREATE, form_data(birth_date=birth.isoformat()))
    assert "guardian_name" in response.context["form"].errors


def test_create_duplicate_needs_confirmation(coordinator_client, make_volunteer):
    existing = make_volunteer(phone="+79031112233")
    response = coordinator_client.post(CREATE, form_data())
    assert response.status_code == 200
    page = response.content.decode()
    assert "Похожие карточки" in page and existing.full_name in page and "телефон" in page
    assert Volunteer.objects.count() == 1
    response = coordinator_client.post(CREATE, form_data(confirm_duplicates="on"))
    assert response.status_code == 302
    assert Volunteer.objects.count() == 2


def test_create_with_photo_shown_inline(coordinator_client):
    photo = SimpleUploadedFile("Кузнецова.png", PNG, content_type="image/png")
    coordinator_client.post(CREATE, form_data(photo_upload=photo))
    v = Volunteer.objects.get()
    assert v.photo is not None and v.photo.content_type == "image/png"
    page = coordinator_client.get(v.get_absolute_url()).content.decode()
    photo_url = page.split('<img src="')[1].split('"')[0]
    response = coordinator_client.get(photo_url)
    assert response.status_code == 200 and response.content == PNG
    assert response["Content-Disposition"].startswith("inline;")


def test_photo_must_be_image(coordinator_client):
    pdf = SimpleUploadedFile("doc.pdf", PDF)
    response = coordinator_client.post(CREATE, form_data(photo_upload=pdf))
    assert response.status_code == 200
    assert "Недопустимый тип" in str(response.context["form"].errors)
    assert not Volunteer.objects.exists()


def test_edit_volunteer(coordinator_client, make_volunteer, refs):
    v = make_volunteer(availability=[(1, datetime.time(9), datetime.time(12))])
    url = reverse("volunteers:edit", args=[v.pk])
    page = coordinator_client.get(url).content.decode()
    assert 'value="09:00"' in page  # доступность подставлена в форму
    data = form_data(
        last_name=v.last_name,
        first_name=v.first_name,
        middle_name=v.middle_name,
        birth_date=v.birth_date.isoformat(),
        phone=v.phone,
        email="",
        programs=[str(refs["program"].pk)],
    )
    response = coordinator_client.post(url, data)
    assert response.status_code == 302
    v.refresh_from_db()
    assert list(v.programs.all()) == [refs["program"]] and v.city == "Москва"
    assert list(v.availability.values_list("weekday", flat=True)) == [5]
    log = AuditLog.objects.filter(action=AuditAction.UPDATE, object_id=str(v.pk)).get()
    assert set(log.fields) == {"availability", "city", "programs"}


# --- Статус, обучение, часы, согласия ---------------------------------------------------------


def test_status_change_page(coordinator_client, make_volunteer, refs):
    v = make_volunteer()
    url = reverse("volunteers:status", args=[v.pk])
    page = coordinator_client.get(url).content.decode()
    assert "Собеседование" in page and "Активен" not in page  # только допустимые переходы
    response = coordinator_client.post(url, {"to_status": "interview"})
    assert response.status_code == 302
    v.refresh_from_db()
    assert v.status == VolunteerStatus.INTERVIEW


def test_status_change_to_left_requires_reason(coordinator_client, make_volunteer, refs):
    v = make_volunteer()
    url = reverse("volunteers:status", args=[v.pk])
    response = coordinator_client.post(url, {"to_status": "left"})
    assert "reason" in response.context["form"].errors
    coordinator_client.post(url, {"to_status": "left", "reason": str(refs["reason"].pk)})
    v.refresh_from_db()
    assert v.status == VolunteerStatus.LEFT
    # Из «Выбыл» переходов нет — страница смены статуса недоступна
    response = coordinator_client.get(url)
    assert response.status_code == 302


def test_activation_blocked_without_consent_shows_reason(coordinator_client, make_volunteer, refs):
    v = make_volunteer()
    services.add_training(v, training_type=refs["training"], completed_on=TODAY())
    services.change_status(v, "interview")
    services.change_status(v, "training")
    url = reverse("volunteers:status", args=[v.pk])
    response = coordinator_client.post(url, {"to_status": "active"})
    assert response.status_code == 200
    assert "согласия" in str(response.context["form"].non_field_errors())
    v.refresh_from_db()
    assert v.status == VolunteerStatus.TRAINING


def test_training_and_hours_pages(coordinator_client, make_volunteer, refs):
    v = make_volunteer()
    coordinator_client.post(
        reverse("volunteers:training_add", args=[v.pk]),
        {"training_type": str(refs["training"].pk), "completed_on": TODAY().isoformat()},
    )
    response = coordinator_client.post(
        reverse("volunteers:hours_add", args=[v.pk]),
        {"worked_on": TODAY().isoformat(), "hours": "3.5", "comment": "Фестиваль"},
    )
    assert response.status_code == 302
    assert v.trainings.count() == 1 and v.manual_hours.get().comment == "Фестиваль"
    page = coordinator_client.get(v.get_absolute_url()).content.decode()
    assert "Вводный инструктаж" in page and "3,5" in page


def test_hours_without_comment_rejected(coordinator_client, make_volunteer):
    v = make_volunteer()
    response = coordinator_client.post(
        reverse("volunteers:hours_add", args=[v.pk]),
        {"worked_on": TODAY().isoformat(), "hours": "2", "comment": ""},
    )
    assert "comment" in response.context["form"].errors
    assert not v.manual_hours.exists()


def test_consent_with_scan_and_revoke(coordinator_client, make_volunteer):
    v = make_volunteer()
    scan = SimpleUploadedFile("согласие.pdf", PDF)
    response = coordinator_client.post(
        reverse("volunteers:consent_add", args=[v.pk]),
        {
            "kind": "general",
            "signed_on": TODAY().isoformat(),
            "signer": "subject",
            "scan": scan,
        },
    )
    assert response.status_code == 302
    consent = v.consents.get()
    assert consent.scan is not None and consent.is_valid_on()
    page = coordinator_client.get(v.get_absolute_url()).content.decode()
    assert "действует" in page
    scan_url = page.split('">Скан</a>')[0].rsplit('href="', 1)[1]
    assert coordinator_client.get(scan_url).content == PDF

    revoke_url = reverse("volunteers:consent_revoke", args=[v.pk, consent.pk])
    response = coordinator_client.post(revoke_url, {"revoked_on": TODAY().isoformat()})
    assert response.status_code == 302
    consent.refresh_from_db()
    assert consent.revoked_on == TODAY() and not consent.is_valid_on()
    assert consent.history.count() == 2  # создание и отзыв
    assert "отозвано" in coordinator_client.get(v.get_absolute_url()).content.decode()


def test_consent_of_other_volunteer_not_reachable(coordinator_client, make_volunteer):
    v1, v2 = make_volunteer(), make_volunteer()
    consent = give_consent(v1)
    url = reverse("volunteers:consent_revoke", args=[v2.pk, consent.pk])
    assert coordinator_client.get(url).status_code == 404


def test_archive_and_restore(coordinator_client, make_volunteer):
    v = make_volunteer()
    coordinator_client.post(reverse("volunteers:archive", args=[v.pk]))
    v.refresh_from_db()
    assert v.is_archived
    assert coordinator_client.get(LIST).context["page"].paginator.count == 0
    assert coordinator_client.get(LIST, {"archived": "1"}).context["page"].paginator.count == 1
    coordinator_client.post(reverse("volunteers:restore", args=[v.pk]))
    v.refresh_from_db()
    assert not v.is_archived
    actions = set(AuditLog.objects.filter(object_id=str(v.pk)).values_list("action", flat=True))
    assert {AuditAction.ARCHIVE, AuditAction.RESTORE} <= actions


def test_archived_card_forms_redirect(coordinator_client, make_volunteer):
    v = make_volunteer()
    services.archive_volunteer(v)
    for name in ("edit", "status", "training_add", "hours_add", "consent_add"):
        response = coordinator_client.get(reverse(f"volunteers:{name}", args=[v.pk]))
        assert response.status_code == 302 and response["Location"] == v.get_absolute_url(), name
    # Отзыв согласия — можно и для архивной карточки
    consent = give_consent(v)
    revoke = reverse("volunteers:consent_revoke", args=[v.pk, consent.pk])
    assert coordinator_client.get(revoke).status_code == 200


# --- ФТ-В-04: разбор пометки «возможный дубль» -------------------------------------------------


def _flagged_pair(make_volunteer):
    original = make_volunteer(last_name="Оригиналова", phone="+79035550011")
    duplicate = make_volunteer(last_name="Копиева", phone="+79035550011")
    Volunteer.objects.filter(pk=duplicate.pk).update(is_possible_duplicate=True)
    duplicate.refresh_from_db()
    return original, duplicate


def test_card_lists_matches_with_reasons(coordinator_client, make_volunteer):
    original, duplicate = _flagged_pair(make_volunteer)
    page = coordinator_client.get(duplicate.get_absolute_url()).content.decode()
    assert "Возможный дубль" in page
    assert original.get_absolute_url() in page and "Оригиналова" in page
    assert "совпадает: <strong>телефон</strong>" in page
    assert "Это другой человек" in page and "Это тот же человек" in page


def test_different_person_clears_flag(coordinator_client, make_volunteer):
    _, duplicate = _flagged_pair(make_volunteer)
    url = reverse("volunteers:duplicate_resolve", args=[duplicate.pk])
    response = coordinator_client.post(url, {"decision": "different"})
    assert response.status_code == 302
    duplicate.refresh_from_db()
    assert not duplicate.is_possible_duplicate and not duplicate.is_archived
    log = AuditLog.objects.filter(action=AuditAction.UPDATE, object_id=str(duplicate.pk)).get()
    assert log.extra == {"duplicate": "different_person"}
    assert (
        "Возможный дубль"
        not in coordinator_client.get(duplicate.get_absolute_url()).content.decode()
    )


def test_same_person_archives_card(coordinator_client, make_volunteer):
    original, duplicate = _flagged_pair(make_volunteer)
    url = reverse("volunteers:duplicate_resolve", args=[duplicate.pk])
    coordinator_client.post(url, {"decision": "same"})
    duplicate.refresh_from_db()
    original.refresh_from_db()
    assert duplicate.is_archived and duplicate.is_possible_duplicate
    assert not original.is_archived
    actions = set(
        AuditLog.objects.filter(object_id=str(duplicate.pk)).values_list("action", flat=True)
    )
    assert AuditAction.ARCHIVE in actions


def test_duplicate_resolve_rules(coordinator_client, make_user, verified_client, make_volunteer):
    _, duplicate = _flagged_pair(make_volunteer)
    url = reverse("volunteers:duplicate_resolve", args=[duplicate.pk])
    assert coordinator_client.get(url).status_code == 405  # только POST
    coordinator_client.post(url, {"decision": "maybe"})  # неизвестное решение
    duplicate.refresh_from_db()
    assert duplicate.is_possible_duplicate
    # Куратор не принимает решение и не видит блок
    curator = verified_client(make_user("curator"))
    assert curator.post(url, {"decision": "different"}).status_code == 403
    assert "Возможный дубль" not in curator.get(duplicate.get_absolute_url()).content.decode()
    # Карточка без пометки — сервис отказывает
    clean = make_volunteer()
    with pytest.raises(services.ValidationError, match="не помечена"):
        services.resolve_duplicate(clean, same_person=False)


def test_filter_only_duplicates(coordinator_client, make_volunteer, make_user, verified_client):
    _flagged_pair(make_volunteer)
    assert _names(coordinator_client.get(LIST, {"duplicates": "1"})) == ["Копиева"]
    # У куратора такого фильтра нет — параметр игнорируется
    curator = verified_client(make_user("curator"))
    assert len(_names(curator.get(LIST, {"duplicates": "1"}))) == 2


def test_get_does_not_archive(coordinator_client, make_volunteer):
    v = make_volunteer()
    assert coordinator_client.get(reverse("volunteers:archive", args=[v.pk])).status_code == 405


# --- Список: поиск, фильтры, сортировка, сохранение (ФТ-В-05, п. 3.10) ------------------------


def _names(response):
    return [v.last_name for v in response.context["page"].object_list]


def test_search_by_name_parts_and_phone(coordinator_client, make_volunteer):
    make_volunteer(last_name="Соколова", first_name="Дарья", phone="+79037770011")
    make_volunteer(last_name="Орлова", first_name="Дарья", phone="+79037770022")
    assert _names(coordinator_client.get(LIST, {"q": "дарья СОКОЛОВА"})) == ["Соколова"]
    assert _names(coordinator_client.get(LIST, {"q": "8 903 777-00-22"})) == ["Орлова"]
    assert len(_names(coordinator_client.get(LIST, {"q": "Дарья"}))) == 2


def test_filters_status_district_skills_program(coordinator_client, make_volunteer, refs):
    a = make_volunteer(
        last_name="Альфа",
        district=refs["district"],
        skills=[refs["skill_drive"], refs["skill_med"]],
        programs=[refs["program"]],
    )
    make_volunteer(last_name="Бета", district=refs["district2"], skills=[refs["skill_drive"]])
    services.change_status(a, "interview")

    assert _names(coordinator_client.get(LIST, {"status": "interview"})) == ["Альфа"]
    assert _names(coordinator_client.get(LIST, {"district": refs["district2"].pk})) == ["Бета"]
    both = {"skills": [refs["skill_drive"].pk, refs["skill_med"].pk]}
    assert _names(coordinator_client.get(LIST, both)) == ["Альфа"]  # все отмеченные навыки
    drive = {"skills": [refs["skill_drive"].pk]}
    assert _names(coordinator_client.get(LIST, drive)) == ["Альфа", "Бета"]
    assert _names(coordinator_client.get(LIST, {"program": refs["program"].pk})) == ["Альфа"]


def test_filter_by_availability(coordinator_client, make_volunteer):
    make_volunteer(last_name="Утро", availability=[(2, datetime.time(9), datetime.time(12))])
    make_volunteer(last_name="Вечер", availability=[(2, datetime.time(18), datetime.time(21))])
    make_volunteer(last_name="Суббота", availability=[(5, datetime.time(9), datetime.time(21))])
    assert _names(coordinator_client.get(LIST, {"weekday": "2"})) == ["Вечер", "Утро"]
    assert _names(coordinator_client.get(LIST, {"weekday": "2", "time": "19:00"})) == ["Вечер"]
    assert _names(coordinator_client.get(LIST, {"time": "10:00"})) == ["Суббота", "Утро"]


def test_sorting_and_pagination(coordinator_client, make_volunteer):
    for name in ["Яковлева", "Абрамова", "Морозова"]:
        make_volunteer(last_name=name)
    assert _names(coordinator_client.get(LIST)) == ["Абрамова", "Морозова", "Яковлева"]
    assert _names(coordinator_client.get(LIST, {"sort": "-name"}))[0] == "Яковлева"
    assert _names(coordinator_client.get(LIST, {"sort": "-created"}))[0] == "Морозова"
    for i in range(25):
        make_volunteer(last_name=f"Ж{i:02d}")
    page2 = coordinator_client.get(LIST, {"page": 2}).context["page"]
    assert page2.number == 2 and len(page2.object_list) == 3


def test_filters_saved_and_restored(make_user, verified_client, make_volunteer):
    user = make_user("coordinator")
    client = verified_client(user)
    client.get(LIST, {"status": "interview", "q": "Иванова", "page": "1"})
    saved = SavedListFilter.objects.get(user=user, list_key="volunteers")
    assert saved.query == "status=interview"  # без текста поиска и номера страницы

    # Новая сессия (после выхода) — фильтры восстанавливаются
    response = verified_client(user).get(LIST)
    assert response.status_code == 302 and response["Location"] == f"{LIST}?status=interview"

    # «Сбросить» удаляет сохранённое
    client.get(LIST, {"reset": "1"})
    assert not SavedListFilter.objects.filter(user=user).exists()
    assert client.get(LIST).status_code == 200


def test_saved_filters_are_personal(make_user, verified_client):
    first = make_user("coordinator")
    verified_client(first).get(LIST, {"status": "active"})
    assert verified_client(make_user("coordinator")).get(LIST).status_code == 200


def test_htmx_returns_results_fragment(coordinator_client, make_volunteer):
    make_volunteer(last_name="Фрагментова")
    response = coordinator_client.get(LIST, {"status": ""}, HTTP_HX_REQUEST="true")
    page = response.content.decode()
    assert response.status_code == 200
    assert "Фрагментова" in page and "<html" not in page
    assert "HX-Request" in response.get("Vary", "")


def test_htmx_history_restore_returns_full_page(coordinator_client):
    response = coordinator_client.get(
        LIST, {"status": ""}, HTTP_HX_REQUEST="true", HTTP_HX_HISTORY_RESTORE_REQUEST="true"
    )
    assert "<html" in response.content.decode()


def test_invalid_filter_value_does_not_crash(coordinator_client, make_volunteer):
    make_volunteer()
    response = coordinator_client.get(LIST, {"status": "nonsense", "district": "x", "time": "25"})
    assert response.status_code == 200
