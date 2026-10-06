"""Уведомления в интерфейсе (ФТ-У-02)."""

import pytest
from django.urls import reverse

from apps.notifications.models import Notification, NotificationKind
from apps.notifications.services import notify, users_with_role

pytestmark = pytest.mark.django_db

LIST = reverse("notifications:list")
KIND = NotificationKind.NEW_VOLUNTEER_APPLICATION


def test_bell_shows_unread_count(make_user, verified_client):
    user = make_user("coordinator")
    notify([user, user], KIND, "Новая анкета", url="/volunteers/")
    page = verified_client(user).get(reverse("core:home")).content.decode()
    assert "Уведомления: непрочитанных 2" in page


def test_open_marks_read_and_redirects(make_user, verified_client):
    user = make_user("coordinator")
    [note] = notify([user], KIND, "Новая анкета", url="/volunteers/")
    client = verified_client(user)
    response = client.post(reverse("notifications:open", args=[note.pk]))
    assert response.status_code == 302 and response["Location"] == "/volunteers/"
    note.refresh_from_db()
    assert note.read_at is not None


def test_open_requires_post(make_user, verified_client):
    user = make_user("coordinator")
    [note] = notify([user], KIND, "Новая анкета", url="/volunteers/")
    assert (
        verified_client(user).get(reverse("notifications:open", args=[note.pk])).status_code == 405
    )


def test_external_url_not_followed(make_user, verified_client):
    user = make_user("coordinator")
    [note] = notify([user], KIND, "x", url="https://evil.example.com/")
    response = verified_client(user).post(reverse("notifications:open", args=[note.pk]))
    assert response["Location"] == LIST


def test_cannot_open_others_notification(make_user, verified_client):
    owner, other = make_user("coordinator"), make_user("coordinator")
    [note] = notify([owner], KIND, "x")
    response = verified_client(other).post(reverse("notifications:open", args=[note.pk]))
    assert response.status_code == 404
    assert not Notification.objects.visible_to(other).exists()
    note.refresh_from_db()
    assert note.read_at is None


def test_read_all(make_user, verified_client):
    user, other = make_user("coordinator"), make_user("coordinator")
    notify([user, user, other], KIND, "x")
    verified_client(user).post(reverse("notifications:read_all"))
    assert not Notification.objects.visible_to(user).unread().exists()
    assert Notification.objects.visible_to(other).unread().count() == 1


def test_list_only_own(make_user, verified_client):
    user, other = make_user("coordinator"), make_user("coordinator")
    notify([user], KIND, "Моё")
    notify([other], KIND, "Чужое")
    page = verified_client(user).get(LIST).content.decode()
    assert "Моё" in page and "Чужое" not in page


def test_users_with_role_includes_superuser_as_admin(make_user):
    from apps.accounts.models import User

    root = User.objects.create_superuser("root", password="x" * 12)
    admin = make_user("admin")
    make_user("admin", is_active=False)
    assert set(users_with_role("admin")) == {root, admin}


def test_list_closed_for_non_staff(make_user, verified_client):
    assert verified_client(make_user("volunteer")).get(LIST).status_code == 403
