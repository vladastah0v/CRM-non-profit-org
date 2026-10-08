from celery import shared_task

from . import reminders


@shared_task(name="volunteers.send_document_reminders")
def send_document_reminders() -> int:
    """Ежедневно: напоминания об истекающих документах волонтёров (ФТ-В-09, ФТ-У-04)."""
    return reminders.send_document_reminders()


@shared_task(name="volunteers.send_idle_digest")
def send_idle_digest() -> int:
    """Еженедельно: сводка о волонтёрах без смен больше 60 дней (ФТ-У-05)."""
    return reminders.send_idle_digest()
