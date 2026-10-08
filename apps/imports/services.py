"""Жизненный цикл импорта: загрузка → проверка (предпросмотр) → импорт → отчёт.

Проверка и импорт идут в фоне (Celery, п. 7.3). Изменения в базе делаются от имени
того, кто загрузил файл (created_by, журнал аудита). Файл удаляется сразу после
завершения или отмены — данные из него остаются только в карточках.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.consents.files import delete_attachment, read_attachment, store_bytes
from apps.core.context import acting_as
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify

from . import importers, reader
from .models import ImportJob, ImportKind, ImportStatus

logger = logging.getLogger(__name__)

CONTENT_TYPES = {
    reader.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    reader.CSV: "text/csv",
}


@transaction.atomic
def start(kind: str, upload: UploadedFile, *, request: HttpRequest | None = None) -> ImportJob:
    """Принять файл: проверить размер и формат, сохранить зашифрованным, начать проверку."""
    if kind not in ImportKind.values:
        raise ValidationError({"kind": "Выберите, что импортировать."})
    if upload.size is None or upload.size > reader.MAX_FILE_SIZE:
        raise ValidationError({"file": "Файл больше 5 МБ — разделите его на части."})
    data = upload.read()
    if not data:
        raise ValidationError({"file": "Файл пустой."})
    file_format = reader.detect_format(upload.name or "", data[:8])
    attachment = store_bytes(
        data, name=upload.name or "import", content_type=CONTENT_TYPES[file_format]
    )
    job = ImportJob.objects.create(kind=kind, file=attachment, file_format=file_format)
    log_event(AuditAction.CREATE, request=request, obj=job, extra={"kind": kind})
    _enqueue("check", job)
    return job


def _enqueue(step: str, job: ImportJob) -> None:
    from .tasks import check_file, import_file

    task = check_file if step == "check" else import_file
    pk = str(job.pk)
    transaction.on_commit(lambda: task.delay(pk))


def _cleanup(job: ImportJob) -> None:
    attachment = job.file
    if attachment is not None:
        job.file = None
        job.save(update_fields=["file"])
        delete_attachment(attachment)


def _table(job: ImportJob) -> reader.Table:
    if job.file is None:
        raise ValidationError("Файл уже удалён.")
    return reader.read_table(read_attachment(job.file), job.file_format)


def _finish_message(job: ImportJob, title: str) -> None:
    if job.created_by is not None:
        notify(
            [job.created_by], NotificationKind.IMPORT_FINISHED, title, url=job.get_absolute_url()
        )


def _fail(job: ImportJob, message: str) -> None:
    job.status = ImportStatus.FAILED
    job.failure = message[:500]
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "failure", "finished_at"])
    _cleanup(job)
    _finish_message(job, "Импорт: файл не удалось прочитать")


def check(job_id: Any) -> None:
    """Предпросмотр: сколько будет создано и обновлено, ошибки построчно, дубли."""
    job = ImportJob.objects.select_related("created_by", "file").filter(pk=job_id).first()
    if job is None or job.status != ImportStatus.CHECKING:
        return
    user: User | None = job.created_by
    with acting_as(user):
        try:
            table = _table(job)
            result = importers.preview(importers.IMPORTERS[job.kind](user), table)  # type: ignore[arg-type]
        except ValidationError as exc:
            _fail(job, " ".join(exc.messages))
            return
        except Exception:
            logger.exception("Импорт %s: сбой проверки", job.pk)
            _fail(job, "Не удалось проверить файл. Проверьте его по шаблону и загрузите снова.")
            return
        updated = ImportJob.objects.filter(pk=job.pk, status=ImportStatus.CHECKING).update(
            status=ImportStatus.READY,
            total_rows=result.summary["rows"],
            preview=result.summary,
            errors=result.errors,
            duplicates=result.duplicates,
        )
        if updated:  # не отменён, пока шла проверка
            _finish_message(job, "Импорт: файл проверен")


@transaction.atomic
def confirm(job: ImportJob, *, request: HttpRequest | None = None) -> None:
    locked = ImportJob.objects.select_for_update().get(pk=job.pk)
    if locked.status != ImportStatus.READY:
        raise ValidationError("Импорт можно запустить только после проверки файла.")
    if not (locked.preview.get("create") or locked.preview.get("update")):
        raise ValidationError("В файле нет строк, которые можно импортировать.")
    locked.status = ImportStatus.IMPORTING
    locked.save(update_fields=["status"])
    log_event(AuditAction.UPDATE, request=request, obj=locked, fields=["status"])
    _enqueue("import", locked)
    job.status = locked.status


def execute(job_id: Any) -> None:
    """Импорт: строки без ошибок сохраняются, ошибочные — в отчёт построчно."""
    job = ImportJob.objects.select_related("created_by", "file").filter(pk=job_id).first()
    if job is None or job.status != ImportStatus.IMPORTING:
        return
    user: User | None = job.created_by
    with acting_as(user):
        try:
            table = _table(job)
            result = importers.run(importers.IMPORTERS[job.kind](user), table)  # type: ignore[arg-type]
        except ValidationError as exc:
            _fail(job, " ".join(exc.messages))
            return
        except Exception:
            logger.exception("Импорт %s: сбой", job.pk)
            _fail(job, "Импорт прерван из-за ошибки. Уже сохранённые строки остались в базе.")
            return
        job.status = ImportStatus.DONE
        job.result = result.summary
        job.errors = result.errors
        job.duplicates = result.duplicates
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "result", "errors", "duplicates", "finished_at"])
        log_event(AuditAction.UPDATE, obj=job, fields=["status"], extra=result.summary)
        _cleanup(job)
        _finish_message(job, "Импорт завершён")


@transaction.atomic
def cancel(job: ImportJob, *, request: HttpRequest | None = None) -> None:
    locked = ImportJob.objects.select_for_update().get(pk=job.pk)
    if locked.status not in (ImportStatus.CHECKING, ImportStatus.READY):
        raise ValidationError("Этот импорт уже нельзя отменить.")
    locked.status = ImportStatus.CANCELLED
    locked.finished_at = timezone.now()
    locked.save(update_fields=["status", "finished_at"])
    _cleanup(locked)
    log_event(AuditAction.UPDATE, request=request, obj=locked, fields=["status"])
    job.status = locked.status
