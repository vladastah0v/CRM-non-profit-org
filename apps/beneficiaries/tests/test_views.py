"""Экраны модуля «Подопечные»: создание, карточка, история, действия, список."""

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditAction, AuditLog
from apps.beneficiaries import services
from apps.beneficiaries.models import Beneficiary, CaseStatus, CuratorAccess
from apps.consents.models import ConsentKind
from apps.core.tests.markup import check_markup

from .conftest import give_consent, to_in_work, years_ago

pytestmark = pytest.mark.django_db

LIST = reverse("beneficiaries:list")
CREATE = reverse("beneficiaries:create")
TODAY = timezone.localdate
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def url(name, b, *args):
    return reverse(f"beneficiaries:{name}", args=[b.pk, *args])


def form_data(**fields):
    return {
        "last_name": "Морозова",
        "first_name": "Галина",
        "middle_name": "Петровна",
        "birth_date": "1950-03-01",
        "address": "Москва, ул. Садовая, 3",
        "phone": "+7 903 222-33-44",
    } | fields


@pytest.fixture
def curator_client(verified_client, curator):
    return verified_client(curator)


def test_curator_creates_own_case(curator_client, curator, brefs):
    response = curator_client.post(
        CREATE, form_data(category=str(brefs["category"].pk), programs=[str(brefs["program"].pk)])
    )
    b = Beneficiary.objects.get()
    assert response.status_code == 302 and response["Location"] == b.get_absolute_url()
    assert b.curator == curator and b.category == brefs["category"]
    assert list(b.programs.all()) == [brefs["program"]]


def test_admin_assigns_curator_on_create(make_user, verified_client, curator):
    client = verified_client(make_user("admin"))
    page = client.get(CREATE).content.decode()
    assert "Ответственный куратор" in page
    client.post(CREATE, form_data(curator=str(curator.pk)))
    assert Beneficiary.objects.get().curator == curator


def test_duplicate_confirmation(curator_client, make_beneficiary):
    make_beneficiary(phone="+79032223344")
    response = curator_client.post(CREATE, form_data())
    assert "Похожие карточки" in response.content.decode()
    assert Beneficiary.objects.count() == 1
    curator_client.post(CREATE, form_data(confirm_duplicates="on"))
    assert Beneficiary.objects.count() == 2


def test_edit_and_timeline(curator_client, make_beneficiary, brefs):
    b = make_beneficiary()
    response = curator_client.post(
        url("edit", b),
        form_data(
            last_name=b.last_name,
            first_name=b.first_name,
            middle_name=b.middle_name,
            birth_date=b.birth_date.isoformat(),
            phone=b.phone,
            address="Москва, ул. Новая, 7",
        ),
    )
    assert response.status_code == 302
    b.refresh_from_db()
    assert b.address == "Москва, ул. Новая, 7"
    give_consent(b)
    services.add_related_person(b, {"full_name": "Морозов Олег", "relation": "сын"})
    page = curator_client.get(url("detail", b)).content.decode()
    # ФТ-П-05: лента — создание, изменение (без значений), согласие, связанное лицо
    for event in (
        "Карточка создана",
        "Изменена карточка",
        "адрес",
        "Добавлено согласие",
        "Добавлено связанное лицо",
    ):
        assert event in page, event


def test_edit_blocked_in_work_without_consent(curator_client, make_beneficiary):
    b = to_in_work(make_beneficiary())
    # Срок согласия истёк, пока случай в работе
    b.consents.update(
        signed_on=TODAY() - timedelta(days=30), valid_until=TODAY() - timedelta(days=1)
    )
    page = curator_client.get(url("detail", b)).content.decode()
    assert "Изменения карточки заблокированы" in page
    response = curator_client.post(
        url("edit", b),
        form_data(
            last_name=b.last_name,
            first_name=b.first_name,
            birth_date=b.birth_date.isoformat(),
            phone=b.phone,
        ),
    )
    assert "без действующего согласия" in str(response.context["form"].non_field_errors())


def test_status_page(curator_client, make_beneficiary, brefs):
    b = make_beneficiary()
    give_consent(b)
    curator_client.post(url("status", b), {"to_status": "assessment"})
    page = curator_client.get(url("status", b)).content.decode()
    assert "В работе" in page
    curator_client.post(url("status", b), {"to_status": "in_work"})
    b.refresh_from_db()
    assert b.status == CaseStatus.IN_WORK
    response = curator_client.post(url("status", b), {"to_status": "closed"})
    assert {"reason", "outcome"} <= set(response.context["form"].errors)
    curator_client.post(
        url("status", b),
        {"to_status": "closed", "reason": str(brefs["reason"].pk), "outcome": "Помогли"},
    )
    b.refresh_from_db()
    assert b.status == CaseStatus.CLOSED
    assert curator_client.get(url("status", b)).status_code == 302  # из «Закрыт» переходов нет


def test_status_page_explains_in_work_problems(curator_client, make_beneficiary):
    b = make_beneficiary()
    services.change_status(b, "assessment")
    page = curator_client.get(url("status", b)).content.decode()
    assert "Перевод в «В работе» сейчас невозможен" in page and "согласия" in page


def test_sensitive_edit_and_documents(curator_client, make_beneficiary):
    b = make_beneficiary()
    response = curator_client.post(url("sensitive_edit", b), {"health": "Астма", "snils": ""})
    assert "health" in response.context["form"].errors  # нет согласия на особые категории
    give_consent(b, kind=ConsentKind.SPECIAL)
    curator_client.post(url("sensitive_edit", b), {"health": "Астма", "snils": "11223344595"})
    response = curator_client.post(
        url("document_add", b),
        {"kind": "passport", "title": "Скан", "upload": SimpleUploadedFile("паспорт.pdf", PDF)},
    )
    assert response.status_code == 302
    page = curator_client.get(url("sensitive", b)).content.decode()
    assert "Астма" in page and "112-233-445 95" in page and "Паспорт" in page
    download = page.split('">Скачать</a>')[0].rsplit('href="', 1)[1]
    assert curator_client.get(download).content == PDF
    # В ленте истории — какие поля изменены, но не их значения
    card = curator_client.get(url("detail", b)).content.decode()
    assert "Изменены защищённые данные" in card and "здоровье, СНИЛС" in card
    assert "Загружен документ" in card
    assert "Астма" not in card and "112-233" not in card


def test_related_person_pages(curator_client, make_beneficiary):
    b = make_beneficiary()
    curator_client.post(
        url("related_add", b),
        {
            "full_name": "Морозова Анна",
            "relation": "дочь",
            "phone": "+79031234567",
            "is_legal_representative": "on",
        },
    )
    person = b.related_persons.get()
    assert person.is_legal_representative
    curator_client.post(
        url("related_edit", b, person.pk), {"full_name": "Морозова Анна", "relation": "дочь"}
    )
    person.refresh_from_db()
    assert not person.is_legal_representative
    curator_client.post(url("related_archive", b, person.pk))
    person.refresh_from_db()
    assert person.is_archived


def test_consent_for_minor_defaults_to_representative(curator_client, make_beneficiary):
    b = make_beneficiary(birth_date=years_ago(12))
    services.add_related_person(
        b, {"full_name": "Петрова Мария", "relation": "мать", "is_legal_representative": True}
    )
    page = curator_client.get(url("consent_add", b)).content.decode()
    assert 'value="Петрова Мария"' in page


def test_consent_add_and_revoke_pauses(curator_client, make_beneficiary, make_user):
    make_user("admin")
    b = to_in_work(make_beneficiary())
    consent = b.consents.get()
    curator_client.post(url("consent_revoke", b, consent.pk), {"revoked_on": TODAY().isoformat()})
    b.refresh_from_db()
    assert b.status == CaseStatus.PAUSED
    response = curator_client.post(
        url("consent_add", b),
        {
            "kind": "general",
            "signed_on": TODAY().isoformat(),
            "signer": "subject",
            "scan": SimpleUploadedFile("согласие.pdf", PDF),
        },
    )
    assert response.status_code == 302 and b.consents.count() == 2


def test_curators_page(make_user, verified_client, make_beneficiary, curator):
    b = make_beneficiary()
    second = make_user("curator", last_name="Второва")
    client = verified_client(make_user("admin"))
    client.post(url("curators", b), {"action": "grant", "user": str(second.pk)})
    access = CuratorAccess.objects.get(beneficiary=b)
    assert access.user == second
    assert "Второва" in client.get(url("detail", b)).content.decode()
    client.post(url("access_revoke", b, access.pk))
    access.refresh_from_db()
    assert access.is_archived
    client.post(url("curators", b), {"action": "assign", "curator": str(second.pk)})
    b.refresh_from_db()
    assert b.curator == second
    logs = AuditLog.objects.filter(action=AuditAction.PERMISSION_CHANGE, object_id=str(b.pk))
    assert logs.count() == 3


def test_archive_restore(curator_client, make_beneficiary):
    b = make_beneficiary()
    curator_client.post(url("archive", b))
    b.refresh_from_db()
    assert b.is_archived
    assert curator_client.get(url("edit", b)).status_code == 302
    assert curator_client.get(LIST).context["page"].paginator.count == 0
    assert curator_client.get(LIST, {"archived": "1"}).context["page"].paginator.count == 1
    curator_client.post(url("restore", b))
    b.refresh_from_db()
    assert not b.is_archived


def test_list_filters(make_user, verified_client, make_beneficiary, brefs, curator):
    a = make_beneficiary(last_name="Альфина", category=brefs["category"])
    make_beneficiary(last_name="Бетина", category=brefs["category2"], curator=make_user("curator"))
    services.change_status(a, "assessment")
    client = verified_client(make_user("admin"))

    def names(**params):
        return [b.last_name for b in client.get(LIST, params).context["page"].object_list]

    assert names() == ["Альфина", "Бетина"]
    assert names(status="assessment") == ["Альфина"]
    assert names(category=brefs["category2"].pk) == ["Бетина"]
    assert names(curator=curator.pk) == ["Альфина"]
    assert names(q="альфина") == ["Альфина"]
    assert names(sort="-name") == ["Бетина", "Альфина"]


def test_pages_markup(make_user, verified_client, make_beneficiary, curator):
    b = make_beneficiary()
    give_consent(b)
    person = services.add_related_person(b, {"full_name": "Сын", "relation": "сын"})
    consent = b.consents.get()
    admin = verified_client(make_user("admin"))
    pages = [
        LIST,
        CREATE,
        *(
            url(n, b)
            for n in (
                "detail",
                "edit",
                "status",
                "sensitive",
                "sensitive_edit",
                "document_add",
                "related_add",
                "consent_add",
                "curators",
            )
        ),
        url("related_edit", b, person.pk),
        url("consent_revoke", b, consent.pk),
    ]
    for page in pages:
        response = admin.get(page)
        assert response.status_code == 200, page
        check_markup(response.content.decode())
    coordinator = verified_client(make_user("coordinator"))
    for page in (LIST, url("detail", b)):
        check_markup(coordinator.get(page).content.decode())


def test_recent_events_keeps_newest_and_stable_ties():
    from apps.beneficiaries.selectors import TIMELINE_LIMIT, RecentEvents, TimelineEvent

    recent = RecentEvents()
    now = timezone.now()
    for i in range(2 * TIMELINE_LIMIT):
        recent.append(TimelineEvent(now + timedelta(seconds=i), str(i)))
    assert [event.title for event in recent.result()] == [
        str(i) for i in reversed(range(TIMELINE_LIMIT, 2 * TIMELINE_LIMIT))
    ]
    tied = RecentEvents()
    for i in range(2 * TIMELINE_LIMIT):
        tied.append(TimelineEvent(now, str(i)))
    assert [event.title for event in tied.result()] == [str(i) for i in range(TIMELINE_LIMIT)]
