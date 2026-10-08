"""Экраны смен и визитов; права по матрице 2.2 («Смены и назначения»)."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.tests.markup import check_markup
from apps.shifts import services
from apps.shifts.models import Assignment, AssignmentStatus, Shift
from apps.volunteers.models import Volunteer

from .conftest import DAY, HOUR, NOW, past_shift

pytestmark = pytest.mark.django_db
LIST = reverse("shifts:list")


@pytest.fixture
def coordinator_client(verified_client, coordinator):
    return verified_client(coordinator)


def shift_post(shift_type, **fields):
    starts = (NOW() + 2 * DAY).replace(minute=0, second=0, microsecond=0)
    local = timezone.localtime(starts)  # поле формы — во времени фонда
    data = {
        "title": "Сбор вещей",
        "shift_type": shift_type.pk,
        "starts_at": f"{local:%Y-%m-%dT%H:%M}",
        "ends_at": f"{local + 3 * HOUR:%Y-%m-%dT%H:%M}",
        "address": "Склад фонда",
        "volunteers_needed": "2",
        "description": "",
        "allow_signup": "on",
    }
    return data | fields


# --- доступ ----------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["analyst"])
def test_matrix_no_access_for_analyst(make_user, verified_client, make_shift, role):
    client = verified_client(make_user(role))
    shift = make_shift()
    assert client.get(LIST + "?period=upcoming").status_code == 403
    assert client.get(reverse("shifts:detail", args=[shift.pk])).status_code == 403


def test_matrix_curator_reads_only(verified_client, curator, make_shift, shift_type):
    client = verified_client(curator)
    shift = make_shift()
    response = client.get(LIST + "?period=upcoming")
    assert response.status_code == 200 and shift in response.context["page"].object_list
    page = client.get(reverse("shifts:detail", args=[shift.pk]))
    assert page.status_code == 200
    assert "Подобрать волонтёров" not in page.content.decode()
    assert client.get(reverse("shifts:create")).status_code == 403
    assert client.post(reverse("shifts:pick", args=[shift.pk])).status_code == 403
    assert client.get(reverse("shifts:attendance", args=[shift.pk])).status_code == 403


def test_list_defaults_to_upcoming_and_filters(coordinator_client, make_shift, make_active):
    response = coordinator_client.get(LIST)
    assert response.status_code == 302 and response["Location"] == f"{LIST}?period=upcoming"
    soon = make_shift(volunteers_needed=1)
    later = make_shift(starts_in=3 * DAY)
    old = past_shift(make_shift(starts_in=4 * DAY), started_ago=2 * DAY)
    services.assign_to_shift(soon, make_active())
    upcoming = coordinator_client.get(LIST, {"period": "upcoming"})
    assert list(upcoming.context["page"].object_list) == [soon, later]
    check_markup(upcoming.content.decode())
    short = coordinator_client.get(LIST, {"period": "upcoming", "staffing": "short"})
    assert list(short.context["page"].object_list) == [later]
    past = coordinator_client.get(LIST, {"period": "past"})
    assert list(past.context["page"].object_list) == [old]
    htmx = coordinator_client.get(LIST, {"period": "upcoming"}, HTTP_HX_REQUEST="true")
    assert htmx.templates[0].name == "shifts/_results.html"


# --- ФТ-С-01: создание и изменение -------------------------------------------------------------


def test_fts01_coordinator_creates_shift(coordinator_client, shift_type, refs):
    page = coordinator_client.get(reverse("shifts:create"))
    check_markup(page.content.decode())
    response = coordinator_client.post(
        reverse("shifts:create"), shift_post(shift_type, skills=[refs["skill_drive"].pk])
    )
    shift = Shift.objects.get()
    assert response.status_code == 302 and response["Location"] == shift.get_absolute_url()
    assert shift.volunteers_needed == 2 and list(shift.skills.all()) == [refs["skill_drive"]]
    bad = coordinator_client.post(
        reverse("shifts:create"), shift_post(shift_type, ends_at="2020-01-01T00:00")
    )
    assert bad.status_code == 200 and bad.context["form"].errors


def test_fts01_edit_and_cancel(coordinator_client, make_shift, shift_type):
    shift = make_shift()
    edit_url = reverse("shifts:edit", args=[shift.pk])
    check_markup(coordinator_client.get(edit_url).content.decode())
    starts = timezone.localtime(shift.starts_at)
    data = shift_post(
        shift_type,
        title="Новое название",
        starts_at=f"{starts:%Y-%m-%dT%H:%M}",
        ends_at=f"{starts + 4 * HOUR:%Y-%m-%dT%H:%M}",
    )
    coordinator_client.post(edit_url, data)
    shift.refresh_from_db()
    assert shift.title == "Новое название"
    cancel_url = reverse("shifts:cancel", args=[shift.pk])
    coordinator_client.post(cancel_url, {"reason": "Нет помещения"})
    shift.refresh_from_db()
    assert shift.is_cancelled and shift.cancel_reason == "Нет помещения"
    assert coordinator_client.get(edit_url).status_code == 302


# --- ФТ-С-02, ФТ-С-03: подбор и назначение ---------------------------------------------------


def test_fts03_pick_suggests_matching_volunteers(coordinator_client, make_shift, make_active, refs):
    shift = make_shift(skills=[refs["skill_drive"]], district=refs["district"])
    starts = timezone.localtime(shift.starts_at)
    good = make_active(
        skills=[refs["skill_drive"]],
        district=refs["district"],
        availability=[(starts.weekday(), datetime.time(0, 0), datetime.time(23, 59))],
    )
    no_skill = make_active()
    busy = make_active(skills=[refs["skill_drive"]])
    services.assign_to_shift(make_shift(starts_in=DAY + HOUR), busy)
    url = reverse("shifts:pick", args=[shift.pk])

    response = coordinator_client.get(url)
    check_markup(response.content.decode())
    found = [c.volunteer for c in response.context["candidates"]]
    assert found == [good]  # по умолчанию — только со всеми навыками; занятых нет
    all_candidates = coordinator_client.get(url, {"q": ""})
    found = [c.volunteer for c in all_candidates.context["candidates"]]
    assert found == [good, no_skill]
    candidate = all_candidates.context["candidates"][0]
    assert candidate.has_skills and candidate.available and candidate.same_district

    response = coordinator_client.post(url, {"volunteer": [good.pk, no_skill.pk]})
    assert response.status_code == 302
    assert set(Assignment.objects.filter(shift=shift).values_list("volunteer", flat=True)) == {
        good.pk,
        no_skill.pk,
    }
    detail = coordinator_client.get(reverse("shifts:detail", args=[shift.pk]))
    check_markup(detail.content.decode())
    assert detail.context["card"].occupied == 2


def test_fts04_attendance_sheet(coordinator_client, make_shift, make_active):
    shift = make_shift()
    came = services.assign_to_shift(shift, make_active())
    missed = services.assign_to_shift(shift, make_active())
    url = reverse("shifts:attendance", args=[shift.pk])
    assert coordinator_client.get(url).status_code == 302  # смена ещё не началась
    past_shift(shift)
    page = coordinator_client.get(url)
    check_markup(page.content.decode())
    coordinator_client.post(
        url,
        {
            f"mark_{came.pk}": "yes",
            f"hours_{came.pk}": "3.5",
            f"mark_{missed.pk}": "no",
            f"hours_{missed.pk}": "4",
        },
    )
    came.refresh_from_db()
    missed.refresh_from_db()
    assert (came.status, str(came.hours)) == (AssignmentStatus.ATTENDED, "3.50")
    assert (missed.status, missed.hours) == (AssignmentStatus.NO_SHOW, None)


def test_assignment_status_page(coordinator_client, make_shift, make_active):
    assignment = services.assign_to_shift(make_shift(), make_active())
    url = reverse("shifts:assignment_status", args=[assignment.pk])
    check_markup(coordinator_client.get(url).content.decode())
    coordinator_client.post(url, {"to_status": "cancelled", "comment": "Заболел"})
    assignment.refresh_from_db()
    assert assignment.status == AssignmentStatus.CANCELLED
    assert assignment.status_changes.latest("created_at").comment == "Заболел"


# --- ФТ-С-05: визиты --------------------------------------------------------------------------


def test_fts05_coordinator_assigns_visit_from_card(
    coordinator_client, in_work_beneficiary, make_active
):
    volunteer = make_active()
    card = coordinator_client.get(in_work_beneficiary.get_absolute_url())
    assert "+ Назначить визит" in card.content.decode()
    url = reverse("shifts:visit_create", args=[in_work_beneficiary.pk])
    check_markup(coordinator_client.get(url).content.decode())
    day = timezone.localtime(NOW() + 2 * DAY).date()
    response = coordinator_client.post(
        url,
        {
            "volunteer": volunteer.pk,
            "date": day.isoformat(),
            "time_from": "11:00",
            "time_to": "13:00",
            "task_comment": "Сходить в аптеку",
            "repeat_weeks": "1",
        },
    )
    assert response.status_code == 302
    visits = Assignment.objects.filter(beneficiary=in_work_beneficiary).order_by("visit_starts_at")
    assert visits.count() == 2
    detail = coordinator_client.get(reverse("shifts:visit_detail", args=[visits[0].pk]))
    check_markup(detail.content.decode())
    card = coordinator_client.get(in_work_beneficiary.get_absolute_url())
    check_markup(card.content.decode())
    assert volunteer.full_name in card.content.decode()


def test_fts05_curator_sees_only_own_visits(
    make_user, verified_client, curator, in_work_beneficiary, make_beneficiary, make_active
):
    from .conftest import to_in_work

    starts = (NOW() + DAY).replace(minute=0, second=0, microsecond=0)
    (mine,) = services.create_visits(
        in_work_beneficiary, make_active(), starts_at=starts, ends_at=starts + HOUR
    )
    other_case = to_in_work(make_beneficiary(curator=make_user("curator")))
    (foreign,) = services.create_visits(
        other_case, make_active(), starts_at=starts, ends_at=starts + HOUR
    )
    client = verified_client(curator)
    assert client.get(reverse("shifts:visit_detail", args=[mine.pk])).status_code == 200
    assert client.get(reverse("shifts:visit_detail", args=[foreign.pk])).status_code == 404
    card = client.get(in_work_beneficiary.get_absolute_url()).content.decode()
    assert "Визиты волонтёров" in card and "+ Назначить визит" not in card
    assert "Назначен визит волонтёра" in card  # лента истории (ФТ-П-05)


def test_visit_cannot_be_assigned_for_new_case(coordinator_client, make_beneficiary):
    beneficiary = make_beneficiary()
    response = coordinator_client.get(reverse("shifts:visit_create", args=[beneficiary.pk]))
    assert response.status_code == 302


# --- карточка волонтёра: смены и бот ------------------------------------------------------------


def test_volunteer_card_shows_shift_stats_and_bot_code(coordinator_client, make_shift, make_active):
    volunteer = make_active()
    shift = make_shift()
    assignment = services.assign_to_shift(shift, volunteer)
    past_shift(shift)
    assignment.shift = shift
    services.change_assignment_status(assignment, AssignmentStatus.NO_SHOW)
    url = reverse("volunteers:detail", args=[volunteer.pk])
    page = coordinator_client.get(url)
    check_markup(page.content.decode())
    shifts = page.context["shifts"]
    assert (shifts.no_show, shifts.attended, shifts.bot_linked) == (1, 0, False)

    response = coordinator_client.post(
        reverse("volunteers:bot_code", args=[volunteer.pk]), follow=True
    )
    text = response.content.decode()
    assert "Код привязки бота:" in text
    volunteer.refresh_from_db()
    assert volunteer.bot_link_code and volunteer.bot_link_code not in text  # в базе — HMAC


def test_curator_does_not_see_bot_controls(verified_client, curator, make_active):
    volunteer = make_active()
    client = verified_client(curator)
    page = client.get(reverse("volunteers:detail", args=[volunteer.pk]))
    assert page.context["shifts"] is None
    response = client.post(reverse("volunteers:bot_code", args=[volunteer.pk]))
    assert response.status_code == 403


def test_bot_unlink_from_card(coordinator_client, make_active):
    volunteer = make_active()
    Volunteer.objects.filter(pk=volunteer.pk).update(telegram_id=777)
    coordinator_client.post(reverse("volunteers:bot_unlink", args=[volunteer.pk]))
    volunteer.refresh_from_db()
    assert volunteer.telegram_id is None


def test_volunteer_list_last_shift_filter(coordinator_client, make_shift, make_active):
    worked = make_active()
    never = make_active()
    shift = make_shift(starts_in=-5 * DAY)
    assignment = services.assign_to_shift(shift, worked)
    services.change_assignment_status(assignment, AssignmentStatus.ATTENDED)
    url = reverse("volunteers:list")
    recent = coordinator_client.get(url, {"last_shift": "30"})
    assert list(recent.context["page"].object_list) == [worked]
    nothing = coordinator_client.get(url, {"last_shift": "never"})
    assert never in nothing.context["page"].object_list
    assert worked not in nothing.context["page"].object_list
