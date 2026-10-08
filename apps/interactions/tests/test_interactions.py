"""Взаимодействия (ФТ-ВЗ-01) и задачи-напоминания (ФТ-ВЗ-02)."""

import datetime

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.beneficiaries import services as beneficiary_services
from apps.core.context import acting_as
from apps.core.tests.markup import check_markup
from apps.interactions import services
from apps.interactions.models import Interaction, Task, TaskStatus
from apps.notifications.models import Notification, NotificationKind

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate


def add_url(name, kind, card):
    return reverse(f"interactions:{name}", args=[kind, card.pk])


def interaction_data(**fields):
    return {
        "kind": "call",
        "happened_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
        "text": "Договорились о визите",
    } | fields


# --- ФТ-ВЗ-01 ----------------------------------------------------------------------------------


def test_curator_logs_interaction_with_own_beneficiary(verified_client, curator, make_beneficiary):
    b = make_beneficiary()
    client = verified_client(curator)
    response = client.post(add_url("add", "beneficiary", b), interaction_data())
    assert response["Location"] == b.get_absolute_url()
    interaction = Interaction.objects.get()
    assert interaction.beneficiary == b and interaction.created_by == curator
    page = client.get(b.get_absolute_url()).content.decode()
    assert "Договорились о визите" in page and "Звонок" in page


def test_coordinator_logs_interaction_with_volunteer(make_user, verified_client, make_volunteer):
    v = make_volunteer()
    client = verified_client(make_user("coordinator"))
    client.post(add_url("add", "volunteer", v), interaction_data(kind="meeting"))
    assert Interaction.objects.get().volunteer == v
    assert "Встреча" in client.get(v.get_absolute_url()).content.decode()


def test_cards_outside_role_are_closed(
    make_user, verified_client, curator, make_beneficiary, make_volunteer
):
    v = make_volunteer()
    b = make_beneficiary()
    foreign = make_beneficiary(curator=make_user("curator"))
    curator_client = verified_client(curator)
    assert curator_client.get(add_url("add", "volunteer", v)).status_code == 403
    assert curator_client.get(add_url("add", "beneficiary", foreign)).status_code == 404
    coordinator = verified_client(make_user("coordinator"))
    assert coordinator.get(add_url("add", "beneficiary", b)).status_code == 403
    analyst = verified_client(make_user("analyst"))
    assert analyst.get(add_url("add", "volunteer", v)).status_code == 404
    assert (
        verified_client(curator)
        .get(reverse("interactions:add", args=["unknown", b.pk]))
        .status_code
        == 404
    )


def test_visibility_by_role(make_user, curator, make_beneficiary, make_volunteer):
    b = make_beneficiary()
    v = make_volunteer()
    services.add_interaction(b, {"kind": "call", "happened_at": timezone.now(), "text": "Б"})
    services.add_interaction(v, {"kind": "call", "happened_at": timezone.now(), "text": "В"})
    assert [i.text for i in Interaction.objects.visible_to(curator)] == ["Б"]
    assert [i.text for i in Interaction.objects.visible_to(make_user("coordinator"))] == ["В"]
    assert Interaction.objects.visible_to(make_user("admin")).count() == 2
    assert not Interaction.objects.visible_to(make_user("analyst")).exists()
    assert not Interaction.objects.visible_to(make_user("curator")).exists()


def test_interaction_validation(make_beneficiary):
    b = make_beneficiary()
    with pytest.raises(ValidationError) as exc:
        services.add_interaction(
            b,
            {
                "kind": "call",
                "happened_at": timezone.now() + datetime.timedelta(hours=1),
                "text": " ",
            },
        )
    assert {"happened_at", "text"} <= set(exc.value.message_dict)


def test_only_author_edits(make_user, verified_client, curator, make_beneficiary):
    b = make_beneficiary()
    second = make_user("curator")
    beneficiary_services.grant_access(b, second)
    with acting_as(curator):
        interaction = services.add_interaction(
            b, {"kind": "visit", "happened_at": timezone.now(), "text": "Визит"}
        )
    edit = reverse("interactions:edit", args=[interaction.pk])
    second_client = verified_client(second)
    assert "Визит" in second_client.get(b.get_absolute_url()).content.decode()  # видит
    assert second_client.get(edit).status_code == 403  # но не правит
    client = verified_client(curator)
    client.post(edit, interaction_data(text="Визит состоялся"))
    interaction.refresh_from_db()
    assert interaction.text == "Визит состоялся"
    client.post(reverse("interactions:archive", args=[interaction.pk]))
    interaction.refresh_from_db()
    assert interaction.is_archived


# --- ФТ-ВЗ-02 ----------------------------------------------------------------------------------


def task_data(assignee, **fields):
    return {"text": "Позвонить", "due_date": TODAY(), "assignee": assignee} | fields


def test_ft_vz_02_task_assignees_limited_to_card(
    make_user, curator, make_beneficiary, make_volunteer
):
    b = make_beneficiary()
    admin = make_user("admin")
    coordinator = make_user("coordinator")
    other_curator = make_user("curator")
    assert set(services.assignees_for(b)) == {curator, admin}
    assert set(services.assignees_for(make_volunteer())) == {coordinator, admin}
    with pytest.raises(ValidationError) as exc:
        services.add_task(b, task_data(other_curator))
    assert "assignee" in exc.value.message_dict


def test_task_notifies_and_closes(make_user, verified_client, curator, make_beneficiary):
    b = make_beneficiary()
    admin = make_user("admin")
    with acting_as(curator):
        task = services.add_task(b, task_data(admin, text="Собрать документы"))
    note = Notification.objects.get(recipient=admin, kind=NotificationKind.TASK_ASSIGNED)
    assert note.url == b.get_absolute_url() and "Собрать" not in note.title + note.body
    with pytest.raises(ValidationError):
        services.add_task(b, task_data(curator, due_date=TODAY() - datetime.timedelta(days=1)))

    client = verified_client(admin)
    client.post(reverse("interactions:task_close", args=[task.pk]), {"result": "done"})
    task.refresh_from_db()
    assert task.status == TaskStatus.DONE and task.done_at is not None
    with pytest.raises(ValidationError, match="уже закрыта"):
        services.close_task(task, done=True)


def test_task_permissions(make_user, verified_client, curator, make_beneficiary):
    b = make_beneficiary()
    beneficiary_services.grant_access(b, second := make_user("curator"))
    task = services.add_task(b, task_data(curator))
    # Второй куратор видит задачу в карточке, но не исполнитель и не автор
    assert Task.objects.visible_to(second).filter(pk=task.pk).exists()
    response = verified_client(second).post(
        reverse("interactions:task_close", args=[task.pk]), {"result": "done"}
    )
    assert response.status_code == 403


def test_task_list_views(make_user, verified_client, curator, make_beneficiary):
    b = make_beneficiary()
    admin = make_user("admin")
    with acting_as(curator):
        services.add_task(b, task_data(curator, text="Моя"))
        services.add_task(b, task_data(admin, text="Поручено админу"))
    overdue = services.add_task(b, task_data(curator, text="Старая"))
    Task.objects.filter(pk=overdue.pk).update(due_date=TODAY() - datetime.timedelta(days=3))
    client = verified_client(curator)
    url = reverse("interactions:tasks")
    mine = [t.text for t in client.get(url).context["page"].object_list]
    assert mine == ["Старая", "Моя"]
    assert "просрочена" in client.get(url).content.decode()
    created = [t.text for t in client.get(url, {"show": "created"}).context["page"].object_list]
    assert "Поручено админу" in created
    check_markup(client.get(url).content.decode())


def test_task_edit_resets_reminder(verified_client, curator, make_beneficiary):
    b = make_beneficiary()
    task = services.add_task(b, task_data(curator))
    services.send_task_reminders()
    task.refresh_from_db()
    assert task.reminded_on == TODAY()
    new_due = TODAY() + datetime.timedelta(days=3)
    client = verified_client(curator)
    response = client.post(
        reverse("interactions:task_edit", args=[task.pk]),
        {"text": "Позвонить ещё раз", "due_date": new_due.isoformat(), "assignee": str(curator.pk)},
    )
    assert response["Location"] == b.get_absolute_url()
    task.refresh_from_db()
    assert task.due_date == new_due and task.reminded_on is None
    # Из списка «Мои задачи» после закрытия — обратно в список
    response = client.post(
        reverse("interactions:task_close", args=[task.pk]), {"result": "cancel", "next": "list"}
    )
    assert response["Location"] == reverse("interactions:tasks")
    task.refresh_from_db()
    assert task.status == TaskStatus.CANCELLED
    assert client.get(reverse("interactions:task_edit", args=[task.pk])).status_code == 302


def test_task_reminders_once(make_user, curator, make_beneficiary):
    b = make_beneficiary()
    services.add_task(b, task_data(curator, text="Сегодня"))
    services.add_task(b, task_data(curator, text="Потом", due_date=TODAY() + datetime.timedelta(5)))
    assert services.send_task_reminders() == 1
    assert services.send_task_reminders() == 0
    note = Notification.objects.get(kind=NotificationKind.TASK_DUE)
    assert note.title == "Сегодня срок задачи" and note.recipient == curator


def test_task_reminder_celery_task(curator, make_beneficiary):
    from apps.interactions.tasks import send_task_reminders

    services.add_task(make_beneficiary(), task_data(curator))
    assert send_task_reminders.delay().get() == 1


def test_timeline_includes_interactions_and_tasks(verified_client, curator, make_beneficiary):
    b = make_beneficiary()
    services.add_interaction(b, {"kind": "visit", "happened_at": timezone.now(), "text": "Визит"})
    task = services.add_task(b, task_data(curator, text="Купить лекарства"))
    services.close_task(task, done=True)
    page = verified_client(curator).get(b.get_absolute_url()).content.decode()
    for event in ("Поставлена задача", "Задача выполнена", "Визит"):
        assert event in page, event


def test_pages_markup(make_user, verified_client, curator, make_beneficiary, make_volunteer):
    b = make_beneficiary()
    v = make_volunteer()
    interaction = services.add_interaction(
        b, {"kind": "call", "happened_at": timezone.now(), "text": "Звонок"}
    )
    task = services.add_task(b, task_data(curator))
    admin = verified_client(make_user("admin"))
    for page in (
        add_url("add", "beneficiary", b),
        add_url("add", "volunteer", v),
        add_url("task_add", "beneficiary", b),
        reverse("interactions:edit", args=[interaction.pk]),
        reverse("interactions:task_edit", args=[task.pk]),
        v.get_absolute_url(),
        b.get_absolute_url(),
    ):
        response = admin.get(page)
        assert response.status_code == 200, page
        check_markup(response.content.decode())
