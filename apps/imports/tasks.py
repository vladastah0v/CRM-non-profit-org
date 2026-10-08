from celery import shared_task

from . import services


@shared_task(name="imports.check_file", ignore_result=True)
def check_file(job_id: str) -> None:
    """Проверка файла и предпросмотр (п. 7.3): в очередь уходит только id импорта."""
    services.check(job_id)


@shared_task(name="imports.import_file", ignore_result=True)
def import_file(job_id: str) -> None:
    services.execute(job_id)
