from celery import shared_task

from . import anonymize


@shared_task(name="consents.anonymize_expired")
def anonymize_expired() -> dict[str, int]:
    """НФТ-ПД-06: ежемесячно обезличить данные с истёкшим сроком хранения, отчёт админам."""
    return anonymize.anonymize_expired()
