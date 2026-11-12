import datetime

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from . import services
from .models import IN_PROGRESS, ImportJob, ImportStatus


@shared_task(name="imports.check_file", ignore_result=True, reject_on_worker_lost=True)
def check_file(job_id: str) -> None:
    """Проверка файла и предпросмотр (п. 7.3): в очередь уходит только id импорта."""
    services.check(job_id)


@shared_task(name="imports.import_file", ignore_result=True, reject_on_worker_lost=True)
def import_file(job_id: str) -> None:
    services.execute(job_id)


@shared_task(name="imports.recover_pending", ignore_result=True)
def recover_pending() -> int:
    """Возвращает в очередь старые задания, потерянные при сбое брокера или worker."""
    with transaction.atomic():
        jobs = list(
            ImportJob.objects.select_for_update(skip_locked=True)
            .filter(
                status__in=IN_PROGRESS,
                created_at__lt=timezone.now() - datetime.timedelta(minutes=15),
            )
            .order_by("created_at")[:100]
        )
        for job in jobs:
            services._enqueue("check" if job.status == ImportStatus.CHECKING else "import", job)
        return len(jobs)
