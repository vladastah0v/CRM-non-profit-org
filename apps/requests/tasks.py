from celery import shared_task

from . import services


@shared_task(name="requests.send_deadline_reminders")
def send_deadline_reminders() -> int:
    """Ежедневно (Celery Beat): напоминания о сроках заявок (ФТ-У-03)."""
    return services.send_deadline_reminders()
