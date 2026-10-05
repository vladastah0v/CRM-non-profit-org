"""Роли и права (раздел 2 ТЗ, ФТ-А-01): матрица 2.2 для объектов ядра."""

import pytest
from django.urls import reverse

from apps.accounts.models import Role, User, UserRole
from apps.accounts.roles import RoleCode, user_has_role, user_role_codes
from apps.audit.models import AuditAction, AuditLog
from apps.core.models import Skill

pytestmark = pytest.mark.django_db

ADMIN_INDEX = reverse("admin:index")
SKILLS = reverse("admin:core_skill_changelist")
USERS = reverse("admin:accounts_user_changelist")
AUDIT = reverse("admin:audit_auditlog_changelist")


def test_five_roles_seeded():
    assert set(Role.objects.values_list("code", flat=True)) == set(RoleCode.values)


def test_roles_are_summed(make_user):
    user = make_user("coordinator", "curator")
    assert user_role_codes(user) == {"coordinator", "curator"}
    assert user_has_role(user, RoleCode.CURATOR)
    assert not user_has_role(user, RoleCode.ADMIN)


def test_superuser_is_admin(db):
    user = User.objects.create_superuser("root", password="x" * 12)
    assert user_has_role(user, RoleCode.ADMIN)


def test_inactive_user_has_no_roles(make_user):
    user = make_user("admin", is_active=False)
    assert user_role_codes(user) == frozenset()


@pytest.mark.parametrize("role", ["coordinator", "curator", "analyst"])
def test_admin_panel_closed_for_non_admins(role, make_user, verified_client):
    client = verified_client(make_user(role))
    for url in (ADMIN_INDEX, SKILLS, USERS, AUDIT):
        assert client.get(url).status_code == 403, url


def test_admin_panel_requires_2fa(make_user, client):
    user = make_user("admin")
    client.force_login(user)  # пароль введён, кода нет
    response = client.get(ADMIN_INDEX)
    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:otp_verify")


def test_admin_panel_open_for_admin(make_user, verified_client):
    client = verified_client(make_user("admin"))
    for url in (ADMIN_INDEX, SKILLS, USERS, AUDIT):
        assert client.get(url).status_code == 200, url


def test_reference_change_is_audited_and_authored(make_user, verified_client):
    admin = make_user("admin")
    client = verified_client(admin)
    response = client.post(reverse("admin:core_skill_add"), {"name": "Вождение", "sort_order": 10})
    assert response.status_code == 302
    skill = Skill.objects.get(name="Вождение")
    assert skill.created_by == admin and skill.updated_by == admin
    log = AuditLog.objects.get(action=AuditAction.CREATE, object_id=str(skill.pk))
    assert log.object_type == "core.skill" and "name" in log.fields


def test_references_archived_not_deleted(make_user, verified_client):
    client = verified_client(make_user("admin"))
    skill = Skill.objects.create(name="Первая помощь")
    response = client.post(
        SKILLS, {"action": "archive_selected", "_selected_action": [str(skill.pk)]}
    )
    assert response.status_code == 302
    skill.refresh_from_db()
    assert skill.is_archived and skill.archived_at is not None
    # Действия «удалить» в админке нет
    page = client.get(SKILLS).content.decode()
    assert "delete_selected" not in page


@pytest.mark.parametrize(
    ("role", "visible"),
    [
        ("admin", True),
        ("coordinator", True),
        ("curator", True),
        ("analyst", True),
        ("volunteer", False),
    ],
)
def test_references_readable_by_staff_only(role, visible, make_user):
    Skill.objects.create(name="Психология")
    assert Skill.objects.visible_to(make_user(role)).exists() is visible


@pytest.mark.parametrize(
    ("role", "visible"), [("admin", True), ("coordinator", False), ("curator", False)]
)
def test_users_and_audit_visible_to_admin_only(role, visible, make_user):
    user = make_user(role)
    AuditLog.objects.create(action=AuditAction.LOGIN, username="x")
    assert User.objects.visible_to(user).exists() is visible
    assert AuditLog.objects.visible_to(user).exists() is visible


def test_role_change_is_audited(make_user, verified_client):
    admin = make_user("admin")
    target = make_user("coordinator")
    client = verified_client(admin)
    url = reverse("admin:accounts_user_change", args=[target.pk])
    curator = Role.objects.get(code="curator")
    existing = UserRole.objects.get(user=target)
    data = {
        "username": target.username,
        "last_name": target.last_name,
        "first_name": target.first_name,
        "middle_name": "",
        "email": "",
        "is_active": "on",
        "user_roles-TOTAL_FORMS": "2",
        "user_roles-INITIAL_FORMS": "1",
        "user_roles-MIN_NUM_FORMS": "0",
        "user_roles-MAX_NUM_FORMS": "1000",
        "user_roles-0-id": str(existing.pk),
        "user_roles-0-user": str(target.pk),
        "user_roles-0-role": str(existing.role_id),
        "user_roles-1-user": str(target.pk),
        "user_roles-1-role": str(curator.pk),
    }
    response = client.post(url, data)
    assert response.status_code == 302, response.content.decode()[:2000]
    log = AuditLog.objects.get(action=AuditAction.PERMISSION_CHANGE, object_id=str(target.pk))
    assert log.extra == {"added": ["curator"], "removed": []}
    assert UserRole.objects.get(user=target, role=curator).created_by == admin


def test_block_access_action(make_user, verified_client):
    admin = make_user("admin")
    target = make_user("curator")
    client = verified_client(admin)
    client.post(
        USERS, {"action": "block_access", "_selected_action": [str(target.pk), str(admin.pk)]}
    )
    target.refresh_from_db()
    admin.refresh_from_db()
    assert not target.is_active
    assert admin.is_active  # себя заблокировать нельзя
    assert AuditLog.objects.filter(
        action=AuditAction.USER_BLOCKED, object_id=str(target.pk)
    ).exists()


def test_issue_2fa_code_action(make_user, verified_client):
    from apps.accounts.models import SecureTOTPDevice
    from conftest import otp_device

    admin = make_user("admin")
    target = make_user("curator")
    otp_device(target)
    target_client = verified_client(target)
    assert target_client.get(reverse("core:home")).status_code == 200

    response = verified_client(admin).post(
        USERS, {"action": "issue_2fa_code", "_selected_action": [str(target.pk)]}, follow=True
    )
    page = response.content.decode()
    assert "Код подключения 2FA" in page
    target.refresh_from_db()
    assert target.has_enrollment_code()
    assert not SecureTOTPDevice.objects.filter(user=target).exists()
    assert AuditLog.objects.filter(
        action=AuditAction.TWO_FACTOR_RESET, object_id=str(target.pk)
    ).exists()
    # Открытые сессии пользователя теряют подтверждение второго фактора
    assert target_client.get(reverse("core:home")).status_code == 302


def test_totp_devices_not_exposed_in_admin(make_user, verified_client):
    client = verified_client(make_user("admin"))
    page = client.get(ADMIN_INDEX).content.decode()
    assert "otp_totp" not in page and "securetotpdevice" not in page


def test_admin_password_change_uses_audited_view(make_user, verified_client):
    client = verified_client(make_user("admin"))
    response = client.get(reverse("admin:password_change"))
    assert response["Location"] == reverse("accounts:password_change")


def test_api_docs_for_verified_staff_only(make_user, verified_client, client):
    schema = reverse("api-v1:schema")
    assert client.get(schema).status_code == 403
    client.force_login(make_user("coordinator"))  # без 2FA
    assert client.get(schema).status_code == 403
    assert verified_client(make_user("coordinator")).get(schema).status_code == 200
