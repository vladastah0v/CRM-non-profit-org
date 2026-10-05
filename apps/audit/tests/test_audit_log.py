"""Журнал аудита — только добавление (НФТ-ИБ-07)."""

from datetime import timedelta

import pytest
from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory
from django.utils import timezone

from apps.audit.models import AuditAction, AuditLog
from apps.audit.services import log_event

pytestmark = pytest.mark.django_db


def test_log_event_records_request_data(make_user, settings):
    user = make_user("admin")
    request = RequestFactory().get("/", HTTP_USER_AGENT="pytest", REMOTE_ADDR="10.1.2.3")
    request.user = user
    log = log_event(AuditAction.EXPORT, request=request, obj=user, fields=["b", "a"])
    assert log.user == user and log.username == user.username
    assert log.ip == "10.1.2.3" and log.user_agent == "pytest"
    assert log.object_type == "accounts.user" and log.object_id == str(user.pk)
    assert log.fields == ["a", "b"]


def test_real_ip_only_from_trusted_proxy(make_user, settings):
    request = RequestFactory().get("/", REMOTE_ADDR="172.18.0.5", HTTP_X_REAL_IP="203.0.113.7")
    request.user = make_user("admin")
    settings.TRUST_X_REAL_IP = False
    assert log_event(AuditAction.LOGIN, request=request).ip == "172.18.0.5"
    settings.TRUST_X_REAL_IP = True
    assert log_event(AuditAction.LOGIN, request=request).ip == "203.0.113.7"


def test_orm_cannot_change_or_delete():
    log = AuditLog.objects.create(action=AuditAction.LOGIN, username="a")
    log.username = "b"
    with pytest.raises(PermissionError):
        log.save()
    with pytest.raises(PermissionError):
        log.delete()
    with pytest.raises(PermissionError):
        AuditLog.objects.filter(pk=log.pk).update(username="b")
    with pytest.raises(PermissionError):
        AuditLog.objects.all().delete()


def test_database_trigger_blocks_update_and_fresh_delete():
    log = AuditLog.objects.create(action=AuditAction.LOGIN, username="a")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE audit_auditlog SET username = 'b' WHERE id = %s", [log.pk])
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM audit_auditlog WHERE id = %s", [log.pk])
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("TRUNCATE audit_auditlog")
    assert AuditLog.objects.filter(pk=log.pk, username="a").exists()


def test_created_at_set_by_database():
    """Запись «задним числом» невозможна: время ставит база."""
    log = AuditLog.objects.create(
        action=AuditAction.LOGIN, created_at=timezone.now() - timedelta(days=400)
    )
    log = AuditLog.objects.get(pk=log.pk)
    assert timezone.now() - log.created_at < timedelta(minutes=1)


def test_records_older_than_retention_can_be_deleted():
    with connection.cursor() as cursor:
        # Имитация записи годичной давности (в рабочей базе так сделать нельзя)
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute("ALTER TABLE audit_auditlog DISABLE TRIGGER audit_log_set_created_at")
        old = AuditLog.objects.create(
            action=AuditAction.LOGIN, created_at=timezone.now() - timedelta(days=366)
        )
        cursor.execute("ALTER TABLE audit_auditlog ENABLE TRIGGER audit_log_set_created_at")
        cursor.execute("DELETE FROM audit_auditlog WHERE id = %s", [old.pk])
    assert not AuditLog.objects.filter(pk=old.pk).exists()
