from celery import shared_task

from . import anonymize, reminders


@shared_task(name="consents.anonymize_expired")
def anonymize_expired() -> dict[str, int]:
    """НФТ-ПД-06: ежемесячно обезличить данные с истёкшим сроком хранения, отчёт админам."""
    return anonymize.anonymize_expired()


@shared_task(name="consents.send_expiry_reminders")
def send_expiry_reminders() -> int:
    """Ежедневно: напоминания об истекающих согласиях (ФТ-У-04)."""
    return reminders.send_expiry_reminders()
