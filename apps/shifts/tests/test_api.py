"""Внутренний API бота (п. 7.2, 7.5): токен с правами, привязка по коду, только свои данные."""

from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ServiceToken
from apps.accounts.roles import RoleCode, user_has_role
from apps.accounts.tokens import issue_token
from apps.audit.models import AuditAction, AuditLog
from apps.beneficiaries import services as beneficiary_services
from apps.shifts import services
from apps.shifts.models import Assignment, AssignmentStatus
from apps.volunteers import services as volunteer_services
from apps.volunteers.models import Volunteer

from .conftest import DAY, HOUR, NOW, past_shift

BASE = "/api/v1/bot/"
TG = 5550001


@pytest.fixture
def bot_token(db) -> str:
    _, raw = issue_token("Telegram-бот", ["bot"])
    return raw


def bot_client(token: str, telegram_id: int | None = TG) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    if telegram_id is not None:
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_TELEGRAM_USER_ID=str(telegram_id)
        )
    return client


@pytest.fixture
def linked_volunteer(make_active, refs) -> Volunteer:
    volunteer = make_active(skills=[refs["skill_drive"]], district=refs["district"])
    code = volunteer_services.issue_bot_link_code(volunteer)
    return volunteer_services.link_telegram(code, TG)


# ---------------------------------------------------------------------------
# Токен сервиса (п. 7.5)
# ---------------------------------------------------------------------------
def test_p75_requires_service_token(db):
    assert APIClient().get(BASE + "me/").status_code in (401, 403)
    response = bot_client("crm_wrong").get(BASE + "me/")
    assert response.status_code == 401


def test_p75_token_scope_and_revocation(db, linked_volunteer):
    token, raw = issue_token("Интеграция", [])
    assert bot_client(raw).get(BASE + "me/").status_code == 403  # нет права «bot»
    token, raw = issue_token("Бот", ["bot"])
    assert bot_client(raw).get(BASE + "me/").status_code == 200
    token.refresh_from_db()
    assert token.last_used_at is not None
    token.archive()
    assert bot_client(raw).get(BASE + "me/").status_code == 401
    _, raw = issue_token("Бот", ["bot"], expires_at=timezone.now() - HOUR)
    assert bot_client(raw).get(BASE + "me/").status_code == 401


def test_p75_token_stored_as_hash(db, capsys):
    call_command("issue_service_token", "Telegram-бот", "--scope", "bot", "--days", "365")
    out = capsys.readouterr().out
    raw = out.split(": ", 1)[1].split()[0]
    token = ServiceToken.objects.get()
    assert raw.startswith("crm_") and raw not in (token.key_hash, token.key_prefix)
    assert token.scopes == ["bot"] and token.expires_at is not None
    assert AuditLog.objects.filter(action=AuditAction.PERMISSION_CHANGE).exists()


def test_staff_session_cannot_use_bot_api(verified_client, make_user):
    client = verified_client(make_user("admin"))
    response = client.get(BASE + "me/", HTTP_X_TELEGRAM_USER_ID=str(TG))
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Привязка аккаунта (п. 7.2)
# ---------------------------------------------------------------------------
def test_p72_link_by_one_time_code(bot_token, make_active):
    volunteer = make_active()
    code = volunteer_services.issue_bot_link_code(volunteer)
    client = bot_client(bot_token)
    assert client.get(BASE + "me/").json()["code"] == "not_linked"
    response = client.post(BASE + "link/", {"code": code.lower().replace("-", " ")})
    assert response.status_code == 200
    assert response.json() == {"first_name": "Анна"}
    volunteer.refresh_from_db()
    assert volunteer.telegram_id == TG
    assert volunteer.user is not None and user_has_role(volunteer.user, RoleCode.VOLUNTEER)
    assert not volunteer.user.has_usable_password()
    # Код одноразовый
    other = bot_client(bot_token, telegram_id=TG + 1)
    assert other.post(BASE + "link/", {"code": code}).status_code == 400
    me = client.get(BASE + "me/").json()
    assert me["can_sign_up"] is True and me["hours_total"] == "0.00"


def test_p72_link_rejects_bad_expired_and_taken(bot_token, make_active, linked_volunteer):
    client = bot_client(bot_token, telegram_id=TG + 5)
    assert client.post(BASE + "link/", {"code": "AAAA-BBBB"}).status_code == 400
    volunteer = make_active()
    code = volunteer_services.issue_bot_link_code(volunteer)
    Volunteer.objects.filter(pk=volunteer.pk).update(bot_link_code_expires=timezone.now() - HOUR)
    assert client.post(BASE + "link/", {"code": code}).status_code == 400
    # Telegram уже привязан к другой карточке
    code = volunteer_services.issue_bot_link_code(volunteer)
    response = bot_client(bot_token).post(BASE + "link/", {"code": code})
    assert response.status_code == 400
    assert "другой карточке" in response.json()["detail"]


def test_p72_link_attempts_are_throttled(bot_token, db):
    client = bot_client(bot_token, telegram_id=TG + 9)
    statuses = [client.post(BASE + "link/", {"code": "AAAA-BBBB"}).status_code for _ in range(11)]
    assert statuses[:10] == [400] * 10
    assert statuses[10] == 429


def test_left_or_unlinked_volunteer_loses_access(bot_token, linked_volunteer, refs):
    client = bot_client(bot_token)
    assert client.post(BASE + "unlink/").status_code == 200
    linked_volunteer.refresh_from_db()
    assert linked_volunteer.telegram_id is None and not linked_volunteer.user.is_active
    assert client.get(BASE + "me/").status_code == 403
    code = volunteer_services.issue_bot_link_code(linked_volunteer)
    assert client.post(BASE + "link/", {"code": code}).status_code == 200
    volunteer_services.change_status(linked_volunteer, "left", reason=refs["reason"])
    assert client.get(BASE + "me/").status_code == 403


# ---------------------------------------------------------------------------
# Мои смены: подтверждение, отказ, явка (С — только свои)
# ---------------------------------------------------------------------------
def test_my_assignments_confirm_and_decline(bot_token, linked_volunteer, make_shift, make_active):
    mine = services.assign_to_shift(make_shift(), linked_volunteer)
    other = services.assign_to_shift(make_shift(), make_active())
    client = bot_client(bot_token)
    items = client.get(BASE + "assignments/").json()["results"]
    assert [i["id"] for i in items] == [str(mine.pk)]
    assert items[0]["can_confirm"] and items[0]["address"] == "Москва, ул. Мира, д. 5"
    # Чужое назначение — как несуществующее
    assert client.post(f"{BASE}assignments/{other.pk}/confirm/").status_code == 404
    response = client.post(f"{BASE}assignments/{mine.pk}/confirm/")
    assert response.json()["status"] == AssignmentStatus.CONFIRMED
    mine.refresh_from_db()
    change = mine.status_changes.latest("created_at")
    assert change.via_bot is True
    assert change.created_by == linked_volunteer.user
    response = client.post(f"{BASE}assignments/{mine.pk}/decline/")
    assert response.json()["status"] == AssignmentStatus.DECLINED
    response = client.post(f"{BASE}assignments/{mine.pk}/confirm/")
    assert response.status_code == 400 and response.json()["detail"]


def test_attendance_via_bot(bot_token, linked_volunteer, make_shift):
    shift = make_shift()
    assignment = services.assign_to_shift(shift, linked_volunteer)
    client = bot_client(bot_token)
    url = f"{BASE}assignments/{assignment.pk}/attendance/"
    assert client.post(url, {"attended": True}, format="json").status_code == 400  # рано
    past_shift(shift)
    item = client.get(BASE + "assignments/").json()["results"][0]
    assert item["needs_attendance"] and item["default_hours"] == "4.00"
    response = client.post(url, {"attended": True, "hours": "3.5"}, format="json")
    assert response.json()["hours"] == "3.50"
    assert volunteer_services.total_hours(linked_volunteer) == Decimal("3.50")
    # Повторная отметка через бота не меняет уже внесённую
    assert client.post(url, {"attended": False}, format="json").status_code == 400
    assert client.post(url, {"attended": True, "hours": "30"}, format="json").status_code == 400


# ---------------------------------------------------------------------------
# Визит: данные подопечного — только на время назначения (п. 2.3, 7.2)
# ---------------------------------------------------------------------------
def test_p23_visit_details_only_during_assignment(bot_token, linked_volunteer, in_work_beneficiary):
    starts = (NOW() + DAY).replace(minute=0, second=0, microsecond=0)
    (visit,) = services.create_visits(
        in_work_beneficiary,
        linked_volunteer,
        starts_at=starts,
        ends_at=starts + 2 * HOUR,
        task_comment="Помочь с покупками",
    )
    client = bot_client(bot_token)
    item = client.get(BASE + "assignments/").json()["results"][0]
    assert item["kind"] == "visit" and item["address"] is None  # в списке — без адреса
    data = client.get(f"{BASE}assignments/{visit.pk}/").json()
    assert data["visit"] == {
        "name": "Пётр",
        "address": "Москва, ул. Ленина, д. 1",
        "phone": in_work_beneficiary.phone,
        "comment": "Помочь с покупками",
    }
    assert "Смирнов" not in str(data)  # только имя, без фамилии
    log = AuditLog.objects.get(action=AuditAction.VIEW_SENSITIVE)
    assert log.object_id == str(in_work_beneficiary.pk) and log.user == linked_volunteer.user

    # Визит прошёл — данные больше не выдаются
    Assignment.objects.filter(pk=visit.pk).update(
        visit_starts_at=NOW() - 5 * HOUR, visit_ends_at=NOW() - 3 * HOUR
    )
    assert client.get(f"{BASE}assignments/{visit.pk}/").json()["visit"] is None


def test_p23_visit_details_hidden_when_case_paused(
    bot_token, linked_volunteer, in_work_beneficiary
):
    starts = (NOW() + DAY).replace(minute=0, second=0, microsecond=0)
    (visit,) = services.create_visits(
        in_work_beneficiary, linked_volunteer, starts_at=starts, ends_at=starts + HOUR
    )
    beneficiary_services.change_status(in_work_beneficiary, "paused")
    data = bot_client(bot_token).get(f"{BASE}assignments/{visit.pk}/").json()
    assert data["visit"] is None and data["status"] == AssignmentStatus.CANCELLED


# ---------------------------------------------------------------------------
# Запись на смену (ФТ-С-02, п. 7.2: по навыкам и району)
# ---------------------------------------------------------------------------
def test_open_shifts_by_skills_district_and_places(bot_token, linked_volunteer, make_shift, refs):
    fits = make_shift(skills=[refs["skill_drive"]], district=refs["district"])
    any_district = make_shift(starts_in=2 * DAY)
    make_shift(starts_in=3 * DAY, skills=[refs["skill_med"]])  # нет навыка
    make_shift(starts_in=4 * DAY, district=refs["district2"])  # другой район
    make_shift(starts_in=5 * DAY, allow_signup=False)
    make_shift(starts_in=60 * DAY)  # слишком далеко
    full = make_shift(starts_in=6 * DAY, volunteers_needed=1)
    services.assign_to_shift(full, linked_volunteer)
    client = bot_client(bot_token)
    ids = [s["id"] for s in client.get(BASE + "shifts/").json()["results"]]
    assert ids == [str(fits.pk), str(any_district.pk)]

    response = client.post(f"{BASE}shifts/{fits.pk}/signup/")
    assert response.status_code == 201
    assert response.json()["status"] == AssignmentStatus.ASSIGNED
    ids = [s["id"] for s in client.get(BASE + "shifts/").json()["results"]]
    assert ids == [str(any_district.pk)]  # куда записан — больше не предлагается
    assert client.post(f"{BASE}shifts/{fits.pk}/signup/").status_code == 400


def test_paused_volunteer_cannot_sign_up(bot_token, linked_volunteer, make_shift):
    make_shift()
    volunteer_services.change_status(linked_volunteer, "paused")
    data = bot_client(bot_token).get(BASE + "shifts/").json()
    assert data["results"] == [] and "Активен" in data["detail"]
