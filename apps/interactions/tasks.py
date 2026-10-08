from celery import shared_task

from . import services


@shared_task(name="interactions.send_task_reminders")
def send_task_reminders() -> int:
    """Ежедневно (Celery Beat): напоминания о сроках задач (ФТ-ВЗ-02)."""
    return services.send_task_reminders()
