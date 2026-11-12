"""Проверки гонок на отдельных соединениях PostgreSQL."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection

from apps.shifts import services
from apps.shifts.models import Assignment
from apps.shifts.tests.test_acceptance import _flush_with_audit_trigger_paused  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)


def race(first_action, second_action, target, method):
    """Остановить первую операцию после проверки; вторая должна ждать блокировку."""
    checked = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    second_pid = []
    original = getattr(target, method)

    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        if threading.current_thread().name.endswith("_0"):
            checked.set()
            assert release.wait(10)
        return result

    def run(action, *, is_second=False):
        close_old_connections()
        try:
            if is_second:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    second_pid.append(cursor.fetchone()[0])
                second_started.set()
            try:
                action()
            except ValidationError:
                return "conflict"
            return "done"
        finally:
            connection.close()

    with (
        mock.patch.object(target, method, side_effect=pause),
        ThreadPoolExecutor(max_workers=2, thread_name_prefix="race") as pool,
    ):
        a = pool.submit(run, first_action)
        try:
            assert checked.wait(10)
            b = pool.submit(run, second_action, is_second=True)
            assert second_started.wait(10)
            deadline = time.monotonic() + 5
            blocked = False
            while time.monotonic() < deadline and not b.done():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                        second_pid,
                    )
                    blocked = cursor.fetchone() == ("Lock",)
                if blocked:
                    break
                time.sleep(0.01)
            assert blocked, "Вторая операция должна ждать блокировку первой"
        finally:
            release.set()
        return a.result(timeout=10), b.result(timeout=10)


def test_parallel_assignments_to_different_shifts_cannot_overlap(make_shift, make_active):
    volunteer = make_active()
    first = make_shift()
    second = make_shift(starts_at=first.starts_at, ends_at=first.ends_at)
    assert race(
        lambda: services.assign_to_shift(first, volunteer),
        lambda: services.assign_to_shift(second, volunteer),
        services,
        "_check_overlap",
    ) == ("done", "conflict")
    assert Assignment.objects.filter(volunteer=volunteer).count() == 1


def test_parallel_archives_leave_help_for_completed_request(make_beneficiary, curator):
    from apps.core.models import AssistanceType
    from apps.requests import services as request_services
    from apps.requests.models import RequestStatus
    from apps.requests.tests.conftest import assistance_data

    food = AssistanceType.objects.create(name="Набор", unit="шт.")
    beneficiary = make_beneficiary()
    request = request_services.create_request(
        beneficiary, {"assistance_type": food, "description": "Нужен набор"}
    )
    data = assistance_data(food, curator)
    first = request_services.register_assistance(beneficiary, data, help_request=request)
    second = request_services.register_assistance(beneficiary, data, help_request=request)
    request_services.change_status(request, RequestStatus.IN_WORK)
    request_services.change_status(request, RequestStatus.DONE)
    assert race(
        lambda: request_services.archive_assistance(first),
        lambda: request_services.archive_assistance(second),
        request_services,
        "log_event",
    ) == ("done", "conflict")
    assert request.assistance.filter(is_archived=False).count() == 1


def test_parallel_import_delivery_runs_once():
    from apps.imports import importers
    from apps.imports import services as import_services
    from apps.imports.models import ImportJob, ImportStatus
    from apps.imports.tests.conftest import csv_file

    job = import_services.start("volunteers", csv_file("Фамилия;Имя\nТест;Тест"))
    ImportJob.objects.filter(pk=job.pk).update(status=ImportStatus.IMPORTING)
    result = importers.RunResult({"rows": 0}, [], [])
    with mock.patch.object(importers, "run", return_value=result) as run:
        assert race(
            lambda: import_services.execute(job.pk),
            lambda: import_services.execute(job.pk),
            importers,
            "run",
        ) == ("done", "done")
    run.assert_called_once()
    job.refresh_from_db()
    assert job.status == ImportStatus.DONE
